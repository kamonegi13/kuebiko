"""事象ニュースの anchor (原文の逐語の抜き書き) と、その関門 (n20 T5、旗 ``FACT_ANCHOR``)。

設計: data/eval/n20_s26_design_2026_10_29.md §2.2「事象ニュース」。facts・caveats・discrepancies の
各行に、出典 [N] の原文から 20〜120 字の逐語 ``anchor`` を書かせ、コードで検査する:

1. anchor が [N] の本文 (プロンプトで見せた範囲) に実在する — 無ければ行を落とす (出典ずれ)
2. anchor に留保語があれば text にも留保・帰属の語がある — 無ければ 1 回作り直し、だめなら落とす
3. text の数値が anchor に在る — 無ければ落とす (版の上限・件数の脱落・取り違えの捕捉)

anchor は**表示しない・保存しない** (関門を通した後に ``to_plain`` で捨てる)。転載を防ぐ関門
(``verbatim.py``) は text にだけ当たり、anchor には当たらない (anchor はここで消えるため)。
純粋関数のみ (LLM を呼ばない)。

⚠ 固有名詞の照合 (設計の ③ の後半) は未実装: 原文が英語で text が日本語のとき表記が揃わず、
誤検知の測り方が未定のため (T5 の未決事項)。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

from src.assessment.evidence_verify import normalize_for_match
from src.eventnews.models import (
    CAVEATS_MAX,
    DISCREPANCIES_MAX,
    FACTS_MAX,
    KEY_POINTS_MAX,
    UNKNOWNS_MAX,
    EventNewsDraft,
    FactItem,
)
from src.tools.llm_schema import require_all_properties

#: anchor の長さ (字)。学習の関門 (scripts/sft_gate_n20_schemas.py) と同じ値。
ANCHOR_MIN_CHARS = 20
ANCHOR_MAX_CHARS = 120
ANCHORED_SECTIONS: tuple[str, ...] = ("facts", "caveats", "discrepancies")

#: 原文側の留保語 (anchor に在れば text に留保・帰属が要る)。英語は語の境界で照合する。
#: may / might / could は小文字だけ (月名の May を拾わない)。
_HEDGE_EN = re.compile(
    r"\b(?:(?i:allegedly|reportedly|purportedly|claim(?:s|ed)?|suspected|believed|likely|"
    r"possibly|appears?|unconfirmed|not (?:been )?confirmed|according to)|may|might|could)\b"
)
_HEDGE_JA: tuple[str, ...] = (
    "とされ",
    "とみられ",
    "見られ",
    "可能性",
    "疑い",
    "主張",
    "によると",
    "未確認",
    "推定",
    "恐れ",
    "報じ",
)
#: text 側で留保・帰属とみなす語 (日本語の生成文 + 英語の生成文の両方)
_CARRY_JA: tuple[str, ...] = (
    *_HEDGE_JA,
    "としている",
    "と述べ",
    "伝え",
    "指摘",
    "確認されていない",
    "示唆",
    "模様",
    "見込み",
    "という",
)
_ID_TOKEN = re.compile(r"\{I\d+\}")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
#: 数字の直後にこれが来たら照合しない (「120万」は原文の「1.2 million」と字面が揃わない)
_CJK_SCALE = re.compile(r"\s*[万億兆千百]")
#: これ未満の桁の整数は照合しない (「3 件」と原文の「three」が揃わない)
_NUMBER_MIN_DIGITS = 2


class AnchoredFactItem(BaseModel):
    """``FactItem`` + ``anchor``。欄の並びと意味は ``FactItem`` と同じ (anchor を最後に足す)。"""

    model_config = ConfigDict(frozen=True)

    text: str
    source_index: int = 0
    paragraph: int = 1
    section: str = "what"
    anchor: str = ""

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return require_all_properties(dict(handler(core_schema)))

    def to_plain(self) -> FactItem:
        return FactItem(
            text=self.text,
            source_index=self.source_index,
            paragraph=self.paragraph,
            section=self.section,
        )


class AnchoredEventNewsDraft(BaseModel):
    """eventnews-v7 の構造化出力。``EventNewsDraft`` と同じ欄 + 行ごとの anchor。"""

    model_config = ConfigDict(frozen=True)

    headline: str
    bluf: str
    key_points: list[str] = Field(
        default_factory=list, json_schema_extra={"maxItems": KEY_POINTS_MAX}
    )
    facts: list[AnchoredFactItem] = Field(
        default_factory=list, json_schema_extra={"maxItems": FACTS_MAX}
    )
    discrepancies: list[AnchoredFactItem] = Field(
        default_factory=list, json_schema_extra={"maxItems": DISCREPANCIES_MAX}
    )
    caveats: list[AnchoredFactItem] = Field(
        default_factory=list, json_schema_extra={"maxItems": CAVEATS_MAX}
    )
    unknowns: list[str] = Field(default_factory=list, json_schema_extra={"maxItems": UNKNOWNS_MAX})

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return require_all_properties(dict(handler(core_schema)))


@dataclass(frozen=True)
class AnchorIssue:
    """1 行の違反。kind = anchor_length / anchor_not_found / hedge_dropped / number_unsupported。"""

    section: str
    position: int
    kind: str


@dataclass(frozen=True)
class AnchorGateResult:
    """関門を通した後の draft (anchor を捨てた本番の形) と、見つけた違反。"""

    draft: EventNewsDraft
    issues: tuple[AnchorIssue, ...]
    dropped: int

    @property
    def hedge_issues(self) -> int:
        return sum(1 for i in self.issues if i.kind == "hedge_dropped")


def _anchor_found(anchor: str, source_index: int, texts: Mapping[int, str]) -> bool:
    """anchor が出典 [N] の見せた本文に在るか。出典 0 (不在の主張) はいずれかの本文。"""
    needle = normalize_for_match(anchor)
    if not needle:
        return False
    if source_index in texts:
        return needle in normalize_for_match(texts[source_index])
    if source_index == 0:
        return any(needle in normalize_for_match(t) for t in texts.values())
    return False


def has_hedge(text: str) -> bool:
    """原文側の留保語を含むか (anchor に当てる)。"""
    return bool(_HEDGE_EN.search(text)) or any(m in text for m in _HEDGE_JA)


def carries_hedge(text: str) -> bool:
    """生成文が留保・帰属の語を持つか (text に当てる)。"""
    return bool(_HEDGE_EN.search(text)) or any(m in text for m in _CARRY_JA)


def _numbers(text: str, *, skip_scaled: bool) -> set[str]:
    folded = unicodedata.normalize("NFKC", _ID_TOKEN.sub(" ", text))
    out: set[str] = set()
    for m in _NUMBER.finditer(folded):
        if skip_scaled and _CJK_SCALE.match(folded, m.end()):
            continue
        token = m.group(0).replace(",", "")
        if "." not in token and len(token) < _NUMBER_MIN_DIGITS:
            continue
        out.add(token)
    return out


def unsupported_numbers(text: str, anchor: str) -> set[str]:
    """text の数値のうち anchor に無いもの (識別子の番号 ``{In}`` は除く)。"""
    have = {n.replace(",", "") for n in _NUMBER.findall(unicodedata.normalize("NFKC", anchor))}
    return _numbers(text, skip_scaled=True) - have


def line_issues(item: AnchoredFactItem, texts: Mapping[int, str]) -> list[str]:
    """1 行の違反の種類 (空 = 通過)。anchor が不正なら他の検査はしない。"""
    anchor = item.anchor.strip()
    if not ANCHOR_MIN_CHARS <= len(anchor) <= ANCHOR_MAX_CHARS:
        return ["anchor_length"]
    if not _anchor_found(anchor, item.source_index, texts):
        return ["anchor_not_found"]
    kinds: list[str] = []
    if has_hedge(anchor) and not carries_hedge(item.text):
        kinds.append("hedge_dropped")
    if unsupported_numbers(item.text, anchor):
        kinds.append("number_unsupported")
    return kinds


def apply_anchor_gate(
    draft: AnchoredEventNewsDraft, texts: Mapping[int, str], *, drop: bool
) -> AnchorGateResult:
    """3 欄の各行を検査する。``drop`` なら違反の行を落とし、常に anchor を捨てた形で返す。

    ``texts`` は [N] → プロンプトで見せた本文 (``generator.prompt_member_texts``)。
    """
    issues: list[AnchorIssue] = []
    kept: dict[str, list[FactItem]] = {}
    for section in ANCHORED_SECTIONS:
        rows: list[AnchoredFactItem] = getattr(draft, section)
        kept[section] = []
        for pos, item in enumerate(rows):
            kinds = line_issues(item, texts)
            issues.extend(AnchorIssue(section, pos, k) for k in kinds)
            if kinds and drop:
                continue
            kept[section].append(item.to_plain())
    plain = EventNewsDraft(
        headline=draft.headline,
        bluf=draft.bluf,
        key_points=list(draft.key_points),
        facts=kept["facts"],
        discrepancies=kept["discrepancies"],
        caveats=kept["caveats"],
        unknowns=list(draft.unknowns),
    )
    dropped = sum(len(getattr(draft, s)) for s in ANCHORED_SECTIONS) - sum(
        len(v) for v in kept.values()
    )
    return AnchorGateResult(draft=plain, issues=tuple(issues), dropped=dropped)


def hedge_rewrite_hint(result: AnchorGateResult) -> str:
    """留保語の脱落を名指しする書き直しヒント (②の 1 回だけの作り直し)。"""
    where = [f"{i.section}[{i.position}]" for i in result.issues if i.kind == "hedge_dropped"]
    return (
        "次の行は、anchor (原文) が留保・伝聞の語"
        " (allegedly / reportedly / とされる / 可能性 等) を含むのに、text が断定になっています。"
        "text に留保・帰属の語を残してください: " + ", ".join(where)
    )


def issue_counts(issues: tuple[AnchorIssue, ...]) -> dict[str, int]:
    """違反の種類ごとの件数 (log 用。本文は持たない)。"""
    out: dict[str, int] = {}
    for i in issues:
        out[i.kind] = out.get(i.kind, 0) + 1
    return out
