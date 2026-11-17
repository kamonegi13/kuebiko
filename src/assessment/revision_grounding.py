"""改訂 (situation_revisions) の含意・監視指標・欠けている証拠の文の関門 (設計 §4)。

関門が掛かっているのは claim だけ (``claim_guard.guard_claim``) で、含意・指標・欠けている証拠は
無関門だった。指標は前回の全件がプロンプトへ戻るので誤りが持ち越される。誤りの型は数値の捏造
ではなく (a) 別事象・過去事案の文脈の混入、(b) 日本への含意の過剰な拡張、(c) 因果の誇張。

2 旗 (``0|shadow|1``、コードの既定 0 = 出力は従来と同一、compose の既定 shadow):

- ``LEDGER_IMPL_GROUNDING``: 文ごとに (1) 固有名詞・数値・CVE・0x が束ねた根拠・既存の抜粋・
  claim・anchor に在るか、(2) 別の状況の主題 (actor・malware・CVE・campaign の anchor) を
  含まないか、(3) 台帳内部の文 (新着・今回・ソース) を含まないか。指標・欠けている証拠は
  正規化して重複を除く。shadow = ログ ``ledger_impl_ungrounded`` のみ、1 = 違反の文・項目を落とし、
  全部落ちたら前回の値を保つ。
- ``LEDGER_JP_IMPLICATION``: 根拠に日本との接点が無い状況の含意に「日本を含む」「国内」等の
  日本への影響の断定があれば shadow = ログ ``ledger_jp_implication_shadow`` のみ、1 = その文を落とす
  (プロンプト側の注意は ``incremental.with_no_japan_notice``)。

照合の部品は ``claim_grounding.check_grounding`` (表記ゆれ・一般語の扱いを共有)。
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, Protocol

from src.assessment.claim_grounding import GroundingSources, check_grounding
from src.assessment.claim_guard import internal_claim_text
from src.logging_config import get_logger

if TYPE_CHECKING:
    from src.assessment.situation_store import RevisionRow, SituationRow
    from src.synthesis.grounded.passes import ClaimAnalysis

_log = get_logger(__name__)

IMPL_FLAG_ENV = "LEDGER_IMPL_GROUNDING"
JP_FLAG_ENV = "LEDGER_JP_IMPLICATION"
Mode = Literal["0", "shadow", "1"]

#: 別の状況の主題として照合する anchor の型 (人名・組織名は偶然一致が多いので含めない)
_SUBJECT_ANCHOR_TYPES = frozenset(
    {"actor", "actor_provisional", "malware_family", "cve", "campaign"}
)
_MIN_SUBJECT_CHARS = 4
_MIN_NUMBER_DIGITS = 3
_LOG_TOKEN_CHARS = 40
_JP_KEYS = frozenset({"involved_country:JP", "mentioned_country:JP"})
_EVIDENCE_TITLE_CHARS = 600

_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?])")
_NUMBER_RE = re.compile(r"\d[\d,，.]*\d|\d")
_HEX_RE = re.compile(r"0x[0-9a-fA-F]{6,}")
_CJK_RE = re.compile(r"[ぁ-んァ-ヶ一-龥]")
_DEDUP_STRIP = re.compile(r"[\W_]+")

#: 日本への影響の断定の語 (含意の文)。「日本」単独の言及は対象外 (接点が無くても事実の文はあり得る)
JP_IMPLICATION_RE = re.compile(
    r"日本(?:を含む|への|国内|企業|の(?:組織|機関|企業|運用|防衛|重要|政府|官公庁)|に(?:も)?(?:波及|影響|対する))"
    r"|国内(?:の|組織|企業|でも|への)|我が国|本邦"
)


def _mode(env: str) -> Mode:
    v = os.environ.get(env, "0").strip().lower()
    return v if v in ("shadow", "1") else "0"  # type: ignore[return-value]


def impl_grounding_mode() -> Mode:
    """``LEDGER_IMPL_GROUNDING`` (既定 0)。未知の値は 0。"""
    return _mode(IMPL_FLAG_ENV)


def jp_implication_mode() -> Mode:
    """``LEDGER_JP_IMPLICATION`` (既定 0)。未知の値は 0。"""
    return _mode(JP_FLAG_ENV)


def _norm(text: str) -> str:
    return _DEDUP_STRIP.sub("", unicodedata.normalize("NFKC", text).lower())


def split_sentences(text: str) -> list[str]:
    """含意を文に分ける (句点を残す)。空要素は捨てる。"""
    return [s for s in _SENTENCE_SPLIT.split(text) if s.strip()]


def normalized_dedupe(items: Iterable[str]) -> tuple[str, ...]:
    """全角半角・空白・記号の違いだけの重複を除く (先勝ち、空は捨てる)。"""
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        s = it.strip()
        key = _norm(s)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(s)
    return tuple(out)


def _digits_form(text: str) -> str:
    return unicodedata.normalize("NFKC", text).replace(",", "").replace("，", "")


@dataclass(frozen=True)
class Violation:
    kind: str  # ungrounded_term / ungrounded_number / foreign_subject / internal_wording
    token: str


@dataclass(frozen=True)
class GateContext:
    """1 つの状況の照合材料。``own_text`` = 束ねた根拠 + 既存の抜粋 + claim + anchor の値。"""

    situation_id: str
    own_text: str
    foreign_owners: Mapping[str, frozenset[str]]  # 正規化した他状況の主題 → 持ち主の situation_id
    has_japan_link: bool = True


def subject_owner_index(situations: Iterable[SituationRow]) -> dict[str, frozenset[str]]:
    """全状況の主題 anchor の値 (正規化) → 持ち主の situation_id 集合。run ごとに 1 回作る。"""
    owners: dict[str, set[str]] = {}
    for row in situations:
        for key in row.anchors:
            kind, _, value = key.partition(":")
            norm = _norm(value)
            if kind in _SUBJECT_ANCHOR_TYPES and len(norm) >= _MIN_SUBJECT_CHARS:
                owners.setdefault(norm, set()).add(row.situation_id)
    return {k: frozenset(v) for k, v in owners.items()}


def anchor_values_text(anchors: Iterable[str]) -> str:
    """anchor ("type:value") の値を空白区切りで連ねる (照合の材料)。"""
    return " ".join(k.partition(":")[2].replace("_", " ") for k in anchors)


def has_japan_link(
    *, claim: str, anchors: Iterable[str], entity_keys: Iterable[str], evidence_text: str
) -> bool:
    """証拠・anchor・claim のどこかに日本との接点があるか (日本関連性の SSoT を再利用)。

    接点 = 当事国/言及国の JP キー、日本語表記の被害組織 anchor、claim・証拠の見出し部の日本の言及。
    迷えば接点あり (含意を黙らせる側に倒さない)。
    """
    from src.cti.japan_relevance import japan_claim_relevance
    from src.cti.nation_gazetteer import nations_in_text

    anchor_list = list(anchors)
    keys = {*entity_keys, *anchor_list}
    if keys & _JP_KEYS or japan_claim_relevance(claim, keys) != "none":
        return True
    if any(k.startswith("victim_org:") and _CJK_RE.search(k) for k in anchor_list):
        return True
    return "JP" in nations_in_text(evidence_text[: _EVIDENCE_TITLE_CHARS * 8])


def find_violations(text: str, ctx: GateContext, *, check_numbers: bool = False) -> list[Violation]:
    """``text`` の根拠なき固有語・他状況の主題・台帳内部の語を列挙する (出現順、重複なし)。"""
    out: list[Violation] = []
    report = check_grounding(text, GroundingSources(text=ctx.own_text))
    out.extend(Violation("ungrounded_term", t) for t in report.violations)
    own_digits = _digits_form(ctx.own_text)
    own_norm = _norm(ctx.own_text)
    if check_numbers:
        for m in _NUMBER_RE.finditer(_digits_form(text)):
            n = m.group(0)
            if len(re.sub(r"\D", "", n)) >= _MIN_NUMBER_DIGITS and n not in own_digits:
                out.append(Violation("ungrounded_number", n))
    for m in _HEX_RE.finditer(text):
        if m.group(0).lower() not in ctx.own_text.lower():
            out.append(Violation("ungrounded_term", m.group(0)))
    text_norm = _norm(text)
    for subject, owners in ctx.foreign_owners.items():
        if ctx.situation_id in owners or subject in own_norm:
            continue
        if subject in text_norm:
            out.append(Violation("foreign_subject", subject))
    wording = internal_claim_text(text)
    if wording:
        out.append(Violation("internal_wording", wording))
    return list(dict.fromkeys(out))


def _short(token: str) -> str:
    return token[:_LOG_TOKEN_CHARS]


def _filter_items(
    items: tuple[str, ...], field: str, ctx: GateContext, mode: Mode, prev: tuple[str, ...]
) -> tuple[str, ...]:
    kept: list[str] = []
    for item in items:
        violations = find_violations(item, ctx)
        for v in violations:
            _log.warning(
                "ledger_impl_ungrounded",
                situation=ctx.situation_id,
                field=field,
                kind=v.kind,
                token=_short(v.token),
                mode=mode,
            )
        if not violations:
            kept.append(item)
    deduped = normalized_dedupe(kept)
    if len(deduped) != len(kept):
        _log.warning(
            "ledger_impl_ungrounded",
            situation=ctx.situation_id,
            field=field,
            kind="duplicate",
            token=str(len(kept) - len(deduped)),
            mode=mode,
        )
    if mode != "1":
        return items
    return deduped or (prev if items else ())


def _gate_implication(implication: str, ctx: GateContext, impl: Mode, jp: Mode, prev: str) -> str:
    sentences = split_sentences(implication)
    kept: list[str] = []
    for sentence in sentences:
        dropped = False
        if impl != "0":
            violations = find_violations(sentence, ctx, check_numbers=True)
            for v in violations:
                _log.warning(
                    "ledger_impl_ungrounded",
                    situation=ctx.situation_id,
                    field="implication",
                    kind=v.kind,
                    token=_short(v.token),
                    mode=impl,
                )
            dropped = bool(violations) and impl == "1"
        if jp != "0" and not ctx.has_japan_link:
            m = JP_IMPLICATION_RE.search(sentence)
            if m:
                _log.warning(
                    "ledger_jp_implication_shadow",
                    situation=ctx.situation_id,
                    token=_short(m.group(0)),
                    mode=jp,
                )
                dropped = dropped or jp == "1"
        if not dropped:
            kept.append(sentence)
    if len(kept) == len(sentences):
        return implication
    return "".join(kept).strip() or prev


def _prev_list(prev: RevisionRow | None, attr: str) -> tuple[str, ...]:
    if prev is None:
        return ()
    try:
        return tuple(str(x) for x in json.loads(str(getattr(prev, attr)) or "[]"))
    except (ValueError, TypeError):
        return ()


def apply_revision_gates(
    analysis: ClaimAnalysis, ctx: GateContext, prev: RevisionRow | None
) -> ClaimAnalysis:
    """含意・指標・欠けている証拠に関門を掛けた新しい ClaimAnalysis (旗 0 は同じ object)。"""
    impl, jp = impl_grounding_mode(), jp_implication_mode()
    if impl == "0" and jp == "0":
        return analysis
    implication = _gate_implication(
        analysis.implication, ctx, impl, jp, prev.implication if prev else ""
    )
    indicators, missing = analysis.indicators, analysis.missing_evidence
    if impl != "0":
        indicators = _filter_items(
            indicators, "indicators", ctx, impl, _prev_list(prev, "indicators_json")
        )
        missing = _filter_items(
            missing, "missing_evidence", ctx, impl, _prev_list(prev, "missing_json")
        )
    return replace(
        analysis, implication=implication, indicators=indicators, missing_evidence=missing
    )


_BODY_CHARS = 6000


class _ArticleRepo(Protocol):
    def get_article_grounding(self, article_id: str) -> dict[str, Any] | None: ...


class _ExcerptStore(Protocol):
    def evidence_excerpts(self, situation_id: str, *, limit: int = 5) -> list[dict[str, str]]: ...


def build_gate_context(
    *,
    repo: _ArticleRepo,
    store: _ExcerptStore,
    situation_id: str | None,
    row: SituationRow | None,
    claim: str,
    read_aids: Iterable[str],
    entities_by_id: Mapping[str, set[str]],
    owners: Mapping[str, frozenset[str]],
) -> GateContext:
    """束ねた根拠 (見出し・要約・本文) + 既存の抜粋 + claim + anchor から照合の材料を作る。

    ``repo`` は ``get_article_grounding``、``store`` は ``evidence_excerpts`` を持つ
    (RunHistoryRepository / SituationStore)。取れない記事は飛ばす (材料が減る = 違反側に倒れる)。
    """
    aids = list(read_aids)
    parts: list[str] = [claim]
    titles: list[str] = []
    for aid in aids:
        g = repo.get_article_grounding(aid)
        if g:
            titles.append(str(g.get("title", "")))
            parts.append(f"{g.get('title', '')} {str(g.get('text', ''))[:_BODY_CHARS]}")
    if situation_id:
        parts.extend(e["excerpt"] for e in store.evidence_excerpts(situation_id, limit=20))
    keys = {k for aid in aids for k in entities_by_id.get(aid, set())}
    anchors = set(row.anchors) if row is not None else set()
    parts.append(anchor_values_text({*anchors, *keys}))
    return GateContext(
        situation_id=situation_id or "",
        own_text="\n".join(parts),
        foreign_owners=owners,
        has_japan_link=has_japan_link(
            claim=claim, anchors=anchors, entity_keys=keys, evidence_text="\n".join(titles)
        ),
    )
