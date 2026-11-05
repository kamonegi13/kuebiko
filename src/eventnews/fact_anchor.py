"""事象ニュースの anchor (原文の逐語の抜き書き) と、その関門 (n20 T5、旗 ``FACT_ANCHOR``)。

設計: data/eval/n20_s26_design_2026_10_29.md §2.2「事象ニュース」。facts・caveats・discrepancies の
各行に、出典 [N] の原文から 20〜120 字の逐語 ``anchor`` を書かせ、コードで検査する:

1. anchor が [N] の本文 (プロンプトで見せた範囲) に実在する — 無ければ行を落とす (出典ずれ)
2. anchor に留保語があれば text にも留保・帰属の語がある — 無ければ 1 回作り直し、だめなら落とす
3. text の数値 (2 桁以上・小数・版番号・桁の語つき) が同じ [N] の本文 (見せた範囲) に在る —
   無ければ落とす (版の上限・件数の取り違えの捕捉)。月名・数詞・桁の語は正規化してから照合
   (H3 (a)、11-05 利用者判断で anchor 内から本文へ緩めた)。discrepancies は反対側の記事の数値も通す

anchor は**表示しない・保存しない** (関門を通した後に ``to_plain`` で捨てる)。教師の
completion だけは anchor つきの形 (``AnchorGateResult.anchored``、``anchored_completion``)。
転載を防ぐ関門
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
from decimal import Decimal, InvalidOperation

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
#: 数値 (桁区切り・小数・版番号) + 任意の桁の語。数詞・桁の語は照合の前に数値へ畳む。
_SCALED_NUMBER = re.compile(
    r"(\d+(?:[.,]\d+)*)(?:\s*(thousand|million|billion|trillion|[千万億兆百]))?",
    re.IGNORECASE,
)
_SCALES: dict[str, int] = {
    "thousand": 10**3,
    "million": 10**6,
    "billion": 10**9,
    "trillion": 10**12,
    "百": 10**2,
    "千": 10**3,
    "万": 10**4,
    "億": 10**8,
    "兆": 10**12,
}
_MONTHS: dict[str, int] = {
    name: i
    for i, names in enumerate(
        (
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sept", "sep"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ),
        start=1,
    )
    for name in names
}
_MONTH_RE = re.compile(
    r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\b\.?", re.IGNORECASE
)
_NUMBER_WORDS: dict[str, int] = {
    w: i
    for i, w in enumerate(
        [
            "zero",
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
            "eleven",
            "twelve",
            "thirteen",
            "fourteen",
            "fifteen",
            "sixteen",
            "seventeen",
            "eighteen",
            "nineteen",
            "twenty",
        ]
    )
} | {
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
    "hundred": 100,
    "dozen": 12,
}
_NUMBER_WORD_RE = re.compile(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b", re.IGNORECASE)
#: これ未満の桁の整数は照合しない (「3 件」「5 日」は表記の揺れが大きく照合に向かない)
_NUMBER_MIN_DIGITS = 2


def _fold_number_text(text: str) -> str:
    """照合の前の正規化: NFKC・識別子の番号を除く・英語の月名と数詞を数字へ。"""
    folded = unicodedata.normalize("NFKC", _ID_TOKEN.sub(" ", text or ""))
    folded = _MONTH_RE.sub(lambda m: f" {_MONTHS[m.group(1).lower()]} ", folded)
    return _NUMBER_WORD_RE.sub(lambda m: str(_NUMBER_WORDS[m.group(1).lower()]), folded)


def _canonical(token: str, scale: int) -> str | None:
    """数値の正規形 (桁の語を掛けた値)。版番号 (点が 2 つ以上) はそのままの字面。"""
    if token.count(".") > 1:
        return token if scale == 1 else None
    try:
        value = Decimal(token) * scale
    except InvalidOperation:
        return None
    if value == value.to_integral_value():
        return str(int(value))
    return format(value.normalize(), "f")


def numeric_values(text: str, *, checked_only: bool) -> set[str]:
    """text の数値の正規形の集合。

    ``checked_only`` (text 側) は照合の対象 (2 桁以上・小数・版番号・桁の語つき) だけを返す。
    原文側 (``False``) は桁の語を掛けた値と掛けない字面の両方を返す (寛容側)。
    """
    out: set[str] = set()
    for m in _SCALED_NUMBER.finditer(_fold_number_text(text)):
        token = m.group(1).replace(",", "")
        word = (m.group(2) or "").lower()
        scale = _SCALES.get(word, 1)
        digits = token.replace(".", "")
        if checked_only and not ("." in token or len(digits) >= _NUMBER_MIN_DIGITS or scale > 1):
            continue
        canon = _canonical(token, scale)
        if canon is not None:
            out.add(canon)
        if not checked_only and scale > 1:
            raw = _canonical(token, 1)
            if raw is not None:
                out.add(raw)
    return out


class AnchoredFactItem(BaseModel):
    """``FactItem`` + ``anchor``。欄の並びと意味は ``FactItem`` と同じ (anchor を最後に足す)。

    全欄必須 (既定値なし): 構造化出力の schema も全欄 required で、教師の completion の
    正規形 (``completion_json``) は常に 5 欄を持つ。caveats・discrepancies の
    ``paragraph`` / ``section`` は表示に使わないが、v6 の保存形 (FactItem の既定) と同じ
    ``1`` / ``"what"`` を書く (テンプレートの例も同じ)。
    """

    model_config = ConfigDict(frozen=True)

    text: str
    source_index: int
    paragraph: int
    section: str
    anchor: str

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


def completion_json(draft: AnchoredEventNewsDraft) -> str:
    """教師の completion の正規形 (1 つに固定): ``model_dump_json`` (欄はモデルの定義順・
    非 ASCII はそのまま)。本番の body_json (``EventNewsDraft.model_dump_json``) と同じ流儀。"""
    return draft.model_dump_json()


@dataclass(frozen=True)
class AnchorIssue:
    """1 行の違反。kind = anchor_length / anchor_not_found / hedge_dropped / number_unsupported。"""

    section: str
    position: int
    kind: str


@dataclass(frozen=True)
class AnchorGateResult:
    """関門を通した後の draft (anchor を捨てた本番の形)、anchor つきの同じ行、見つけた違反。"""

    draft: EventNewsDraft
    anchored: AnchoredEventNewsDraft
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


def unsupported_numbers(text: str, source: str) -> set[str]:
    """text の数値 (2 桁以上・小数・版番号・桁の語つき) のうち原文 ``source`` に無いもの。

    照合は正規化の後 (NFKC・桁区切り・英語の月名と数詞・thousand/million/万/億 等の桁の語を
    数値に畳む)。識別子の番号 ``{In}`` は除く。
    """
    return numeric_values(text, checked_only=True) - numeric_values(source, checked_only=False)


def number_source_text(section: str, source_index: int, texts: Mapping[int, str]) -> str:
    """数値の照合範囲。facts・caveats は同じ ``source_index`` の記事の本文 (0 はいずれか)。

    discrepancies はソース間の相違を書く欄なので、反対側の数値も同じ規則で
    (提示したいずれかの記事の本文に在れば通す)。
    """
    if section == "discrepancies" or source_index == 0:
        return "\n".join(texts.values())
    return texts.get(source_index, "")


def line_issues(
    item: AnchoredFactItem, texts: Mapping[int, str], *, section: str = "facts"
) -> list[str]:
    """1 行の違反の種類 (空 = 通過)。anchor が不正なら他の検査はしない。"""
    anchor = item.anchor.strip()
    if not ANCHOR_MIN_CHARS <= len(anchor) <= ANCHOR_MAX_CHARS:
        return ["anchor_length"]
    if not _anchor_found(anchor, item.source_index, texts):
        return ["anchor_not_found"]
    kinds: list[str] = []
    if has_hedge(anchor) and not carries_hedge(item.text):
        kinds.append("hedge_dropped")
    if unsupported_numbers(item.text, number_source_text(section, item.source_index, texts)):
        kinds.append("number_unsupported")
    return kinds


def apply_anchor_gate(
    draft: AnchoredEventNewsDraft, texts: Mapping[int, str], *, drop: bool
) -> AnchorGateResult:
    """3 欄の各行を検査する。``drop`` なら違反の行を落とす。

    ``draft`` は anchor を捨てた本番の形 (既存の戻り値)、``anchored`` は同じ行を anchor つきで
    持つ形 (教師の completion 用 — 契約: 関門を通した後で各行に anchor を持つ)。
    ``texts`` は [N] → プロンプトで見せた本文 (``generator.prompt_member_texts``)。
    """
    issues: list[AnchorIssue] = []
    kept: dict[str, list[AnchoredFactItem]] = {}
    for section in ANCHORED_SECTIONS:
        rows: list[AnchoredFactItem] = getattr(draft, section)
        kept[section] = []
        for pos, item in enumerate(rows):
            kinds = line_issues(item, texts, section=section)
            issues.extend(AnchorIssue(section, pos, k) for k in kinds)
            if kinds and drop:
                continue
            kept[section].append(item)
    anchored = draft.model_copy(update={s: list(v) for s, v in kept.items()})
    dropped = sum(len(getattr(draft, s)) for s in ANCHORED_SECTIONS) - sum(
        len(v) for v in kept.values()
    )
    return AnchorGateResult(
        draft=to_plain_draft(anchored), anchored=anchored, issues=tuple(issues), dropped=dropped
    )


def to_plain_draft(draft: AnchoredEventNewsDraft) -> EventNewsDraft:
    """anchor を捨てた本番の形 (識別子関門は anchor を見ない — anchor は生の CVE・IP を含む)。"""
    return EventNewsDraft(
        headline=draft.headline,
        bluf=draft.bluf,
        key_points=list(draft.key_points),
        facts=[f.to_plain() for f in draft.facts],
        discrepancies=[f.to_plain() for f in draft.discrepancies],
        caveats=[f.to_plain() for f in draft.caveats],
        unknowns=list(draft.unknowns),
    )


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
