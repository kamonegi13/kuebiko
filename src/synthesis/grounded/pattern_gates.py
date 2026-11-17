"""パターン版の状況総括の関門 (決定論)。設計 docs/synthesis_pattern_redesign.md §3.4。

LLM が書いた欄 (見出し・パターンごとの文・注視点) を次の順に通し、通らなければ欄ごと決定論の文に
置き換えて件数を残す: 空 → 禁止語 → 長さ (3 文・300 字、超えたら先頭 2 文) →
印との照合 (提供者・委託先の語) → 固有名詞・数値の裏付け (``claim_grounding``)。
ID の関門 (渡した ``pattern_id`` 以外を捨てる・欠けは決定論の文) は ``gate_patterns``。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field

from src.assessment.claim_grounding import GroundingSources, check_grounding
from src.synthesis.grounded.pattern_fallback import fallback_headline, fallback_pattern_text
from src.synthesis.grounded.patterns import Pattern, PatternCase, PatternPlan

MAX_SENTENCES = 3
MAX_CHARS = 300
KEEP_SENTENCES = 2
HEADLINE_MAX_SENTENCES = 2
PROVIDER_WORDS: tuple[str, ...] = (
    "委託先",
    "提供者",
    "サードパーティ",
    "第三者",
    "サプライヤ",
    "取引先経由",
)
UNIVERSAL_WORDS: tuple[str, ...] = ("いずれも", "すべて", "全て", "どれも", "共通して", "いずれの")
_SUBJECT = re.compile(r"^(.{2,16}?)(?:は|が|で|の|に|を|、)")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_SEVERITY_TOKEN = re.compile(r"S[123]")
_SENTENCE_END = re.compile(r"(?<=。)")


@dataclass(frozen=True)
class GateStats:
    """関門の発火件数 (log 用。本文は持たない)。"""

    unknown_ids: int = 0
    missing: int = 0
    banned: int = 0
    length_trimmed: int = 0
    length_replaced: int = 0
    mark_replaced: int = 0
    grounding_replaced: int = 0
    headline_replaced: bool = False
    watch_dropped: bool = False
    #: 関門に掛けた欄の数 (パターンごとの文 + 見出し + 注視点) と、決定論の文に置き換えた数
    fields_total: int = 0
    fields_replaced: int = 0

    @property
    def replaced_rate(self) -> float:
        return self.fields_replaced / self.fields_total if self.fields_total else 0.0


@dataclass(frozen=True)
class GatedTexts:
    headline: str
    pattern_texts: dict[str, str] = field(default_factory=dict)
    watch: str = ""


#: パターンの文で禁じる語 = 断定の言い過ぎだけ (2026-11-17)。slot render の ``BANNED_IN_CLAUSE`` は
#: 確度語・反証・対立仮説も禁じる (あちらは確度の定型文をコードが付けるため) が、パターンの文は
#: 「漏えいした可能性があると公表」のような事実の引用や、入力に付けた確度の印 (低確度 等) を書く。
#: 流用すると 12 窓のほぼ全欄が落ちた (11-17 の P2 の 1 回目、無効の記録は設計書 §6 の追記)。
PATTERN_BANNED: tuple[str, ...] = (
    "確実に",
    "間違いなく",
    "疑いなく",
    "疑いの余地",
    "断言",
    "必ず",
    "明らかに",
)


def has_banned(text: str) -> bool:
    return any(w in text for w in PATTERN_BANNED)


def split_sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_END.split(text.strip()) if s.strip()]


def _case_names(case: PatternCase) -> tuple[str, ...]:
    """文が事案を名指ししているかの照合語: 題の主語 + 題の固有名詞候補。"""
    from src.assessment.claim_grounding import extract_terms

    m = _SUBJECT.match(case.title)
    subject = (m.group(1),) if m else ()
    return (*subject, *extract_terms(case.title))


def mark_violation(text: str, cases: Sequence[PatternCase]) -> bool:
    """提供者・委託先の語が、その印の無い事案・パターンに掛かっていないか (True = 違反)。"""
    for sentence in split_sentences(text):
        if not any(w in sentence for w in PROVIDER_WORDS):
            continue
        named = [c for c in cases if any(n and n in sentence for n in _case_names(c))]
        if any(not c.is_provider for c in named):
            return True
        if not any(c.is_provider for c in cases):
            return True
        if any(u in sentence for u in UNIVERSAL_WORDS) and not all(c.is_provider for c in cases):
            return True
    return False


def _numbers(text: str) -> set[str]:
    norm = _SEVERITY_TOKEN.sub("", unicodedata.normalize("NFKC", text))
    return {n.replace(",", "") for n in _NUMBER.findall(norm)}


def grounding_violation(text: str, source_claims: Sequence[str], mark_numbers: set[str]) -> bool:
    """文中の固有名詞・数値が、パターンの事案の claim か印の件数に無ければ True。"""
    sources = "\n".join(source_claims)
    if check_grounding(text, GroundingSources(text=sources)).violations:
        return True
    return bool(_numbers(text) - _numbers(sources) - mark_numbers)


def _mark_numbers(patterns: Sequence[Pattern], extra: Sequence[int] = ()) -> set[str]:
    nums: set[int] = set(extra)
    for p in patterns:
        nums |= {p.count, p.s3_count, p.provider_count}
        nums |= {n for _, n in p.delta_counts} | {n for _, n in p.confidence_counts}
    return {str(n) for n in nums}


def _trim(text: str, max_sentences: int) -> tuple[str, bool]:
    """3 文・300 字を超えたら先頭 2 文に切る。(結果, 切ったか)。"""
    sentences = split_sentences(text)
    if len(sentences) <= max_sentences and len(text) <= MAX_CHARS:
        return text.strip(), False
    return "".join(sentences[:KEEP_SENTENCES]), True


_COUNT_KEYS = ("banned", "length_trimmed", "length_replaced", "mark_replaced", "grounding_replaced")


def _gate_field(
    raw: str,
    *,
    cases: Sequence[PatternCase],
    patterns: Sequence[Pattern],
    max_sentences: int,
    counts: dict[str, int],
    extra_numbers: Sequence[int] = (),
) -> str | None:
    """1 欄を関門に通す。通ればその文、落とすなら None (呼び手が決定論の文に置き換える)。"""
    text = raw.strip()
    if not text:
        return None
    if has_banned(text):
        counts["banned"] += 1
        return None
    text, trimmed = _trim(text, max_sentences)
    counts["length_trimmed"] += trimmed
    if len(text) > MAX_CHARS:
        counts["length_replaced"] += 1
        return None
    if mark_violation(text, cases):
        counts["mark_replaced"] += 1
        return None
    claims = [c for case in cases for c in case.claims]
    if grounding_violation(text, claims, _mark_numbers(patterns, extra_numbers)):
        counts["grounding_replaced"] += 1
        return None
    return text


def _gate_pattern_texts(
    pattern_items: Sequence[tuple[str, str]], plan: PatternPlan, counts: dict[str, int]
) -> tuple[dict[str, str], int, int, int]:
    """パターンごとの文を ID・本文の関門に通す。(pid → 文, 未知 id 数, 欠け数, 置換数)。"""
    by_id = {p.pattern_id: p for p in plan.patterns}
    got: dict[str, str] = {}
    unknown = 0
    for pid, text in pattern_items:
        if pid not in by_id:
            unknown += 1
        elif pid not in got:
            got[pid] = text
    texts: dict[str, str] = {}
    missing = replaced = 0
    for pid, p in by_id.items():
        gated = None
        if pid in got:
            gated = _gate_field(
                got[pid], cases=p.cases, patterns=[p], max_sentences=MAX_SENTENCES, counts=counts
            )
        else:
            missing += 1
        replaced += gated is None
        texts[pid] = gated if gated is not None else fallback_pattern_text(p)
    return texts, unknown, missing, replaced


def gate_patterns(
    *,
    headline: str,
    pattern_items: Sequence[tuple[str, str]],
    watch: str,
    plan: PatternPlan,
    moved_questions: Sequence[str] = (),
) -> tuple[GatedTexts, GateStats]:
    """LLM の出力 (見出し・(pattern_id, text)・注視点) を関門に通して決定論の文で埋める。"""
    counts = dict.fromkeys(_COUNT_KEYS, 0)
    texts, unknown, missing, replaced = _gate_pattern_texts(pattern_items, plan, counts)
    universe = [c for p in plan.patterns for c in p.cases] + list(plan.rest)
    shared = {
        "cases": universe,
        "patterns": plan.patterns,
        "counts": counts,
        "extra_numbers": [len(plan.patterns), len(plan.rest), plan.moved_count],
    }
    head = _gate_field(headline, max_sentences=HEADLINE_MAX_SENTENCES, **shared)  # type: ignore[arg-type]
    has_watch = bool(watch.strip())
    w = _gate_field(watch, max_sentences=MAX_SENTENCES, **shared)  # type: ignore[arg-type]
    stats = GateStats(
        unknown_ids=unknown,
        missing=missing,
        headline_replaced=head is None,
        watch_dropped=w is None and has_watch,
        fields_total=len(plan.patterns) + 1 + has_watch,
        fields_replaced=replaced + (head is None) + (w is None and has_watch),
        **counts,
    )
    return GatedTexts(
        headline=head if head is not None else fallback_headline(plan, moved_questions),
        pattern_texts=texts,
        watch=w or "",
    ), stats
