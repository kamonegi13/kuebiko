"""判定基準モデル + 保存前検証の unit test (WP1)。

契約 (フィールド名・型・enum) の SSoT が ``SummaryOutput`` 側にあること、判定基準側の
壊れ方 (未知フィールド / 重複 / 空 body / 壊れた few-shot) が **error と warning に
正しく振り分く** ことを固定する。
"""

from __future__ import annotations

from src.pipeline.summary import SummaryOutput
from src.prompts.rubric_model import (
    RubricExample,
    RubricSection,
    SectionKind,
    SummarizerRubric,
    contract_fields,
    summary_output_fields,
    validate_rubric,
)

_VALID_EXAMPLE_JSON = (
    '{"title_ja": "サンプル", "importance": "high", "category": "apt", "summary": "要約"}'
)
# §1-F で記録した既存欠陥と同型: JSON としては正しいが ArticleType に無い値
_SCHEMA_VIOLATING_JSON = (
    '{"title_ja": "サンプル", "importance": "high", "category": "apt", '
    '"summary": "要約", "article_type": "informational"}'
)


def _section(
    field_id: str,
    *,
    kind: SectionKind = "rubric",
    body: str = "判定基準の本文。",
) -> RubricSection:
    return RubricSection(field_id=field_id, kind=kind, title="", body=body, note="")


def test_unknown_field_id_is_error() -> None:
    # Arrange: SummaryOutput に存在しないフィールド名
    rubric = SummarizerRubric(sections=[_section("victim_org")])

    # Act
    result = validate_rubric(rubric)

    # Assert
    assert result.is_valid is False
    assert "未知のフィールドです: victim_org" in result.errors
    assert result.unknown_fields == ("victim_org",)


def test_duplicate_field_id_is_error() -> None:
    rubric = SummarizerRubric(sections=[_section("summary"), _section("summary")])

    result = validate_rubric(rubric)

    assert any("重複" in e for e in result.errors)


def test_rubric_section_with_blank_body_is_error() -> None:
    rubric = SummarizerRubric(sections=[_section("summary", body="   \n  ")])

    result = validate_rubric(rubric)

    assert "kind=rubric の判定基準が空です: summary" in result.errors


def test_suppressed_section_with_blank_body_is_allowed() -> None:
    rubric = SummarizerRubric(sections=[_section("diamond", kind="suppressed", body="")])

    result = validate_rubric(rubric)

    assert result.errors == ()
    assert result.is_valid is True


def test_broken_example_json_is_error() -> None:
    rubric = SummarizerRubric(
        sections=[_section("summary")],
        examples=[RubricExample(label="壊れた例", json_text="{ not json")],
    )

    result = validate_rubric(rubric)

    assert any("JSON が不正です" in e for e in result.errors)


def test_example_failing_schema_is_warning_not_error() -> None:
    rubric = SummarizerRubric(
        sections=[_section("summary")],
        examples=[
            RubricExample(label="正しい例", json_text=_VALID_EXAMPLE_JSON),
            RubricExample(label="型違反の例", json_text=_SCHEMA_VIOLATING_JSON),
        ],
    )

    result = validate_rubric(rubric)

    assert result.errors == ()
    assert any("SummaryOutput 検証に失敗" in w and "article_type" in w for w in result.warnings)


def test_contract_fields_cover_schema_with_enums() -> None:
    fields = contract_fields()

    assert len(fields) == len(SummaryOutput.model_fields) == 24
    by_name = {f.name: f for f in fields}
    assert by_name["importance"].enum == ("high", "medium", "low")
    assert by_name["importance"].required is True
    assert by_name["title_ja"].required is True
    # default 持ち = schema 上は required でない (「必須」は判定基準側の散文で担保する)
    assert by_name["article_type"].required is False
    assert by_name["routing_flags"].json_type == "object"
    assert by_name["analyst_note"].nullable is True
    assert by_name["iocs"].json_type == "array"


def test_missing_fields_lists_undeclared_schema_fields() -> None:
    rubric = SummarizerRubric(sections=[_section("summary")])

    result = validate_rubric(rubric)

    assert set(result.missing_fields) == summary_output_fields() - {"summary"}
    assert "compromise_date" in result.missing_fields
    assert any("宣言の無いフィールド" in w for w in result.warnings)
