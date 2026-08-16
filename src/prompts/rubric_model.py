"""summarizer 判定基準 (rubric) のデータモデルと検証。

**契約 (出力フィールドの名前・型・enum) は ``SummaryOutput`` が SSoT** であり、本 module は
そこから導出するだけで独自の型表を持たない (重複した真実を作らない)。判定基準の側は
利用者が編集する散文なので pydantic で境界検証だけ行う。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.pipeline.summary import SummaryOutput

SectionKind = Literal["rubric", "suppressed"]

MAX_SECTIONS = 40
MAX_EXAMPLES = 10
MAX_BODY_CHARS = 20_000
MAX_EXAMPLE_CHARS = 8_000


class RubricSection(BaseModel):
    """1 出力フィールドの判定基準 (または「出力させない」宣言)。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field_id: str = Field(min_length=1, max_length=64)  # SummaryOutput のフィールド名
    kind: SectionKind = "rubric"
    title: str = Field(default="", max_length=80)  # UI 表示名。**プロンプトには出さない**
    body: str = Field(default="", max_length=MAX_BODY_CHARS)  # プロンプト本体 (verbatim)
    note: str = Field(default="", max_length=500)  # UI 補足。**プロンプトには出さない**


class RubricExample(BaseModel):
    """few-shot 出力例 1 件。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(default="", max_length=120)  # 見出し ``# 出力例 ({label})``
    json_text: str = Field(min_length=1, max_length=MAX_EXAMPLE_CHARS)  # フェンス無しの JSON 本体


class SummarizerRubric(BaseModel):
    """summarizer プロンプトの編集可能層 (rubric config の全体)。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    intro: str = Field(default="", max_length=2_000)
    sections: list[RubricSection] = Field(default_factory=list, max_length=MAX_SECTIONS)
    examples: list[RubricExample] = Field(default_factory=list, max_length=MAX_EXAMPLES)


@dataclass(frozen=True)
class ContractField:
    """schema 由来の契約メタ (UI 表示専用、プロンプトには出さない)。"""

    name: str
    json_type: str  # "string" / "array" / "object" / "boolean" / "integer"
    required: bool  # JSON Schema の required に載っているか
    enum: tuple[str, ...]  # Literal のとき値集合、無ければ空
    nullable: bool


IssueSeverity = Literal["error", "warning"]
IssueCode = Literal[
    "duplicate_field_id",
    "unknown_field",
    "empty_rubric_body",
    "example_json_invalid",
    "example_not_object",
    "example_schema_mismatch",
    "render_failed",
    "missing_fields",
    "composed_longer_than_legacy",
]


@dataclass(frozen=True)
class RubricIssue:
    """検証結果 1 件を **局在化情報つき** で表したもの。

    ``errors`` / ``warnings`` の平坦な文字列列は「どのカードの問題か」を持たないため、
    UI がバナーからカードへ飛べない。issues はその位置情報 (``field_id`` /
    ``example_index`` / ``keys``) を添える **追加** の表現で、文字列列はここから
    同順で導出される (I-3: 文字列と順序は不変)。
    """

    severity: IssueSeverity
    code: IssueCode
    message: str
    field_id: str = ""  # target=section のとき。それ以外は ""
    example_index: int = 0  # target=example のとき 1-based。それ以外は 0
    keys: tuple[str, ...] = ()  # example_schema_mismatch のとき不正だったキー名


@dataclass(frozen=True)
class RubricValidation:
    """保存前検証の結果 (error は保存拒否、warning は保存可だが表示する)。"""

    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    unknown_fields: tuple[str, ...]  # schema に無い field_id
    missing_fields: tuple[str, ...]  # schema にあるが宣言が無い field
    composed_chars: int
    legacy_chars: int
    issues: tuple[RubricIssue, ...] = ()  # errors/warnings の局在化つき表現 (同順)

    @property
    def is_valid(self) -> bool:
        return not self.errors


def summary_output_fields() -> frozenset[str]:
    """LLM に出力させうるフィールド名の全体集合 (契約の SSoT)。"""
    return frozenset(SummaryOutput.model_fields)


def _json_type_of(prop: dict[str, Any]) -> tuple[str, bool]:
    """JSON Schema の 1 property から (型名, nullable) を取り出す。"""
    if "type" in prop:
        return str(prop["type"]), False
    branches = [b for b in prop.get("anyOf", []) if isinstance(b, dict)]
    nullable = any(b.get("type") == "null" for b in branches)
    for branch in branches:
        branch_type = branch.get("type")
        if branch_type and branch_type != "null":
            return str(branch_type), nullable
    return "string", nullable


def contract_fields() -> tuple[ContractField, ...]:
    """``SummaryOutput`` の JSON Schema から契約メタを導出する (UI バッジ用)。"""
    schema = SummaryOutput.model_json_schema()
    required = set(schema.get("required", []))
    fields: list[ContractField] = []
    for name, prop in schema.get("properties", {}).items():
        json_type, nullable = _json_type_of(prop)
        fields.append(
            ContractField(
                name=name,
                json_type=json_type,
                required=name in required,
                enum=tuple(str(v) for v in prop.get("enum", ())),
                nullable=nullable,
            ),
        )
    return tuple(fields)


def _check_sections(rubric: SummarizerRubric) -> tuple[list[RubricIssue], list[str], list[str]]:
    """(issues, unknown_fields, missing_fields) を返す。"""
    known = summary_output_fields()
    issues: list[RubricIssue] = []
    unknown: list[str] = []
    declared: set[str] = set()
    seen: set[str] = set()
    for section in rubric.sections:
        field_id = section.field_id
        if field_id in seen:
            issues.append(
                RubricIssue(
                    severity="error",
                    code="duplicate_field_id",
                    message=f"field_id が重複しています: {field_id}",
                    field_id=field_id,
                ),
            )
        seen.add(field_id)
        if field_id in known:
            declared.add(field_id)
        else:
            unknown.append(field_id)
            issues.append(
                RubricIssue(
                    severity="error",
                    code="unknown_field",
                    message=f"未知のフィールドです: {field_id}",
                    field_id=field_id,
                ),
            )
        if section.kind == "rubric" and not section.body.strip():
            issues.append(
                RubricIssue(
                    severity="error",
                    code="empty_rubric_body",
                    message=f"kind=rubric の判定基準が空です: {field_id}",
                    field_id=field_id,
                ),
            )
    missing = sorted(known - declared)
    return issues, list(dict.fromkeys(unknown)), missing


def _check_examples(rubric: SummarizerRubric) -> list[RubricIssue]:
    """few-shot 例の JSON 妥当性を見る。壊れた JSON = error / schema 不一致 = warning。

    1 例につき issue は高々 1 件なので、error と warning が混在した順序のまま返しても
    severity で射影すれば従来の 2 本のリストと同順になる (I-3)。
    """
    issues: list[RubricIssue] = []
    for index, example in enumerate(rubric.examples, 1):
        label = example.label or f"#{index}"
        try:
            parsed = json.loads(example.json_text)
        except json.JSONDecodeError as e:
            issues.append(
                RubricIssue(
                    severity="error",
                    code="example_json_invalid",
                    message=(
                        f"出力例 {index} ({label}) の JSON が不正です: {e.msg} (行 {e.lineno})"
                    ),
                    example_index=index,
                ),
            )
            continue
        if not isinstance(parsed, dict):
            issues.append(
                RubricIssue(
                    severity="error",
                    code="example_not_object",
                    message=f"出力例 {index} ({label}) が JSON オブジェクトではありません",
                    example_index=index,
                ),
            )
            continue
        try:
            SummaryOutput.model_validate(parsed)
        except ValidationError as e:
            bad_keys = tuple(sorted({str(d["loc"][0]) for d in e.errors() if d.get("loc")}))
            issues.append(
                RubricIssue(
                    severity="warning",
                    code="example_schema_mismatch",
                    message=(
                        f"出力例 {index} ({label}) が SummaryOutput 検証に失敗: "
                        + ", ".join(bad_keys)
                    ),
                    example_index=index,
                    keys=bad_keys,
                ),
            )
    return issues


def _read_legacy_chars(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8"))
    except OSError:
        return 0


def _check_render(rubric: SummarizerRubric, path: Path) -> RubricIssue | None:
    """合成テンプレートを実際に render する (I6: 保存前検証の最後の砦)。

    section body / intro / 例に混入した ``{{`` ``{%`` は compose() で verbatim に
    テンプレートソースへ埋まるため、ここで落とさないと保存後の本番 run で初めて
    TemplateSyntaxError になる。失敗理由は型名のみ (本文をログ/応答に出さない)。
    """
    from src.prompts.sample_article import SAMPLE_ARTICLE
    from src.prompts.summarizer_composer import build_template

    try:
        build_template(rubric, path=path).render(
            article=SAMPLE_ARTICLE,
            body=SAMPLE_ARTICLE.body_text or "",
        )
    except Exception as e:  # noqa: BLE001 — 種類を問わず保存を拒否する
        return RubricIssue(
            severity="error",
            code="render_failed",
            message=f"合成テンプレートのレンダリングに失敗しました: {type(e).__name__}",
        )
    return None


def validate_rubric(
    rubric: SummarizerRubric,
    *,
    legacy_path: Path | None = None,
) -> RubricValidation:
    """保存前検証。合成まで行い legacy との文字数差も返す (枯死リスクの可視化)。"""
    from src.prompts.summarizer_composer import LEGACY_TEMPLATE_PATH, compose

    section_issues, unknown, missing = _check_sections(rubric)
    issues = section_issues + _check_examples(rubric)
    render_issue = _check_render(rubric, legacy_path or LEGACY_TEMPLATE_PATH)
    if render_issue:
        issues.append(render_issue)
    if missing:
        issues.append(
            RubricIssue(
                severity="warning",
                code="missing_fields",
                message="宣言の無いフィールドがあります: " + ", ".join(missing),
            ),
        )

    composed_chars = len(compose(rubric))
    legacy_chars = _read_legacy_chars(legacy_path or LEGACY_TEMPLATE_PATH)
    if legacy_chars and composed_chars > legacy_chars:
        issues.append(
            RubricIssue(
                severity="warning",
                code="composed_longer_than_legacy",
                message=(
                    f"合成後のプロンプトが legacy より {composed_chars - legacy_chars:,} 字"
                    "長くなっています (末尾フィールドの枯死リスク)"
                ),
            ),
        )
    # errors / warnings は issues からの同順の射影 (I-3: 文字列と順序は不変)。
    return RubricValidation(
        errors=tuple(i.message for i in issues if i.severity == "error"),
        warnings=tuple(i.message for i in issues if i.severity == "warning"),
        unknown_fields=tuple(unknown),
        missing_fields=tuple(missing),
        composed_chars=composed_chars,
        legacy_chars=legacy_chars,
        issues=tuple(issues),
    )
