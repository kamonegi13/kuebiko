"""判定基準モデル + 保存前検証の unit test (WP1)。

契約 (フィールド名・型・enum) の SSoT が ``SummaryOutput`` 側にあること、判定基準側の
壊れ方 (未知フィールド / 重複 / 空 body / 壊れた few-shot) が **error と warning に
正しく振り分く** ことを固定する。
"""

from __future__ import annotations

import json

import pytest

from src.pipeline.summary import SummaryOutput
from src.prompts.rubric_model import (
    RubricExample,
    RubricIssue,
    RubricSection,
    RubricValidation,
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


# ---------------------------------------------------------------------------
# issues 層 (WP-B2) — 局在化情報を持つ構造化された検証結果
# ---------------------------------------------------------------------------

_BROKEN_JSON = "{ not json"


def _mixed_rubric() -> SummarizerRubric:
    """error / warning を 1 通りずつ含む複合ケース (issues 導出の回帰基準)。"""
    return SummarizerRubric(
        sections=[
            _section("summary"),
            _section("summary"),  # 重複
            _section("victim_org"),  # 未知フィールド
            _section("category", body="   \n "),  # kind=rubric なのに空
            _section("diamond", kind="suppressed", body=""),  # 空でも正常
        ],
        examples=[
            RubricExample(label="壊れた例", json_text=_BROKEN_JSON),
            RubricExample(label="配列の例", json_text="[]"),
            RubricExample(label="型違反の例", json_text=_SCHEMA_VIOLATING_JSON),
        ],
    )


def _issues_of(result: RubricValidation, code: str) -> tuple[RubricIssue, ...]:
    return tuple(i for i in result.issues if i.code == code)


def test_unknown_field_issue_carries_field_id() -> None:
    # Arrange
    rubric = SummarizerRubric(sections=[_section("victim_org")])

    # Act
    result = validate_rubric(rubric)

    # Assert — バナーから当該カードへ飛ぶための位置情報が乗っている
    found = _issues_of(result, "unknown_field")
    assert len(found) == 1
    assert found[0].severity == "error"
    assert found[0].field_id == "victim_org"
    assert found[0].example_index == 0


def test_example_schema_mismatch_issue_carries_index_and_keys() -> None:
    # Arrange — 3 件目だけが SummaryOutput 検証に失敗する
    rubric = SummarizerRubric(
        sections=[_section("summary")],
        examples=[
            RubricExample(label="正しい例 1", json_text=_VALID_EXAMPLE_JSON),
            RubricExample(label="正しい例 2", json_text=_VALID_EXAMPLE_JSON),
            RubricExample(label="型違反の例", json_text=_SCHEMA_VIOLATING_JSON),
        ],
    )

    # Act
    result = validate_rubric(rubric)

    # Assert — 「どの例の・どのキーが」不正かがキー単位で局在化される
    found = _issues_of(result, "example_schema_mismatch")
    assert len(found) == 1
    assert found[0].severity == "warning"
    assert found[0].example_index == 3
    assert found[0].keys == ("article_type",)
    assert found[0].field_id == ""


def test_empty_rubric_body_issue_carries_field_id() -> None:
    rubric = SummarizerRubric(sections=[_section("summary", body="   \n  ")])

    result = validate_rubric(rubric)

    found = _issues_of(result, "empty_rubric_body")
    assert len(found) == 1
    assert found[0].severity == "error"
    assert found[0].field_id == "summary"


def test_render_failure_issue_is_global() -> None:
    """レンダリング失敗は合成全体の問題なのでカードにも出力例にも紐付かない。"""
    # Arrange — 判定基準の本文に閉じていない Jinja タグが混入したケース
    rubric = SummarizerRubric(sections=[_section("summary", body="壊れた {{ tag")])

    # Act
    result = validate_rubric(rubric)

    # Assert
    found = _issues_of(result, "render_failed")
    assert len(found) == 1
    assert found[0].severity == "error"
    assert found[0].field_id == ""
    assert found[0].example_index == 0
    # 本文をメッセージに含めない (CLAUDE.md §4: プロンプト本文を応答/ログに出さない)
    assert "壊れた" not in found[0].message


def test_errors_and_warnings_are_unchanged_and_derived_from_issues() -> None:
    """I-3: ``errors`` / ``warnings`` の文字列と順序が issues 導入前と完全一致する。

    PUT /rubric の 400 detail と UI の表示がこの文字列に依存しているため、issues は
    **追加** であって置換ではないことを機械的に固定する (回帰ガード)。
    """
    # Arrange — 壊れた JSON のメッセージは stdlib 由来なので実測値から組み立てる
    # (固定は「フォーマット」であって json モジュールの文言ではない)
    rubric = _mixed_rubric()
    with pytest.raises(json.JSONDecodeError) as exc:
        json.loads(_BROKEN_JSON)
    json_detail = f"{exc.value.msg} (行 {exc.value.lineno})"
    missing = sorted(summary_output_fields() - {"summary", "category", "diamond"})

    # Act
    result = validate_rubric(rubric)

    # Assert — 文字列と順序の完全一致
    assert result.errors == (
        "field_id が重複しています: summary",
        "未知のフィールドです: victim_org",
        "kind=rubric の判定基準が空です: category",
        f"出力例 1 (壊れた例) の JSON が不正です: {json_detail}",
        "出力例 2 (配列の例) が JSON オブジェクトではありません",
    )
    assert result.warnings == (
        "出力例 3 (型違反の例) が SummaryOutput 検証に失敗: article_type",
        "宣言の無いフィールドがあります: " + ", ".join(missing),
    )
    # errors / warnings は issues の同順の射影であること (二重の真実を作らない)
    assert tuple(i.message for i in result.issues if i.severity == "error") == result.errors
    assert tuple(i.message for i in result.issues if i.severity == "warning") == result.warnings
