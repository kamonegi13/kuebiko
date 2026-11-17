"""改訂 (situation_revisions) の含意・監視指標・欠けている証拠の文の関門 (設計 §4)。

関門が掛かっているのは claim だけ (``claim_guard.guard_claim``) で、含意・指標・欠けている証拠は
無関門だった。指標は前回の全件がプロンプトへ戻るので誤りが持ち越される。誤りの型は数値の捏造
ではなく (a) 別事象・過去事案の文脈の混入、(b) 日本への含意の過剰な拡張、(c) 因果の誇張。

2 旗 (``0|shadow|1``、コードの既定 0 = 出力は従来と同一、compose の既定 shadow):

- ``LEDGER_IMPL_GROUNDING``: (a) 台帳内部の文 (新着・今回・ソース)、(b) 別の状況の主題
  (actor・malware・CVE・campaign の anchor) は含意・指標・欠けている証拠のすべてで見る。
  含意の文だけ (c) CVE・0x・英数字の固有名詞・3 桁以上の数値が束ねた根拠・既存の抜粋・claim・
  anchor に在るかも見る (カタカナ・漢字の一般語の照合はしない)。指標・欠けている証拠は
  正規化して重複を除く。shadow = ログ ``ledger_impl_ungrounded`` のみ、1 = 違反の文・項目を落とし、
  全部落ちたら前回の値を保つ。
- ``LEDGER_JP_IMPLICATION``: 根拠に日本との接点が無い状況の含意に「日本を含む」「国内」等の
  日本への影響の断定があれば shadow = ログ ``ledger_jp_implication_shadow`` のみ、1 = その文を落とす
  (プロンプト側の注意は ``incremental.with_no_japan_notice``)。
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal, Protocol

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
_LOG_MAX_TOKENS = 10
_JP_KEYS = frozenset({"involved_country:JP", "mentioned_country:JP"})
_EVIDENCE_TITLE_CHARS = 600

_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?])")
_NUMBER_RE = re.compile(r"\d[\d,，.]*\d|\d")
_HEX_RE = re.compile(r"0x[0-9a-fA-F]{6,}")
_CVE_RE = re.compile(r"CVE-\d{4}-\d{4,}", re.IGNORECASE)
_ASCII_TERM = re.compile(r"[A-Za-z][A-Za-z0-9._-]{2,}")
_MIN_ASCII_CHARS = 3
#: 固有名詞として数えない英語の一般語・略語 (小文字)。文頭の大文字語や技術略語の誤発火を避ける
_GENERIC_ASCII: frozenset[str] = frozenset(
    {
        "the", "this", "that", "these", "those", "with", "when", "where", "which", "while",
        "from", "into", "over", "under", "after", "before", "also", "only", "both", "each",
        "some", "such", "their", "there", "then", "than", "they", "them", "have", "has", "had",
        "was", "were", "will", "would", "could", "should", "may", "might", "can", "not", "but",
        "and", "for", "are", "our", "its", "via", "per", "etc", "new", "key", "any", "all",
        "api", "url", "uri", "sms", "sns", "web", "app", "http", "https", "html", "json",
        "xml", "sql", "dns", "vpn", "rce", "xss", "csrf", "ioc", "iocs", "ttp", "ttps", "edr",
        "siem", "soc", "cti", "apt", "cve", "cvss", "cisa", "nist", "mitre", "windows", "linux",
        "macos", "android", "ios", "cloud", "email", "wifi", "ssh", "rdp", "smb", "ftp", "tls",
        "ssl", "ip", "ipv4", "ipv6", "tcp", "udp", "cdn", "waf", "poc", "dos", "ddos", "zero-day",
        "zeroday", "patch", "exploit", "malware", "ransomware", "phishing", "botnet", "backdoor",
    }
)  # fmt: skip
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
    # 項目ごとに作り直さないよう 1 回だけ導出する (own_text が大きいため)
    own_norm: str = field(init=False, default="")
    own_digits: str = field(init=False, default="")
    own_lower: str = field(init=False, default="")

    def __post_init__(self) -> None:
        object.__setattr__(self, "own_norm", _norm(self.own_text))
        object.__setattr__(self, "own_digits", _digits_form(self.own_text))
        object.__setattr__(self, "own_lower", self.own_text.lower())


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


def _ascii_proper_nouns(text: str) -> list[str]:
    """英数字の固有名詞候補 (3 字以上、大文字か数字を含み、一般語・CVE・0x を除く)。"""
    out: list[str] = []
    for m in _ASCII_TERM.finditer(text):
        term = m.group(0).rstrip("._-")
        low = term.lower()
        if len(term) < _MIN_ASCII_CHARS or low in _GENERIC_ASCII or low.startswith(("cve-", "0x")):
            continue
        if any(c.isupper() or c.isdigit() for c in term):
            out.append(term)
    return out


def find_violations(text: str, ctx: GateContext, *, check_terms: bool = False) -> list[Violation]:
    """``text`` の違反を列挙する (出現順、重複なし)。

    常に見る: 他状況の主題 (foreign_subject)・台帳内部の語 (internal_wording)。
    ``check_terms`` (含意の文だけ): CVE・0x・英数字の固有名詞・3 桁以上の数値が根拠に在るか。
    カタカナ・漢字の一般語の照合はしない (将来の観測を書く指標・欠けている証拠や含意の一般語が
    誤発火の大半だったため、2026-11-17)。
    """
    out: list[Violation] = []
    own_norm = ctx.own_norm
    if check_terms:
        for term in [*_CVE_RE.findall(text), *_ascii_proper_nouns(text)]:
            if _norm(term) not in own_norm:
                out.append(Violation("ungrounded_term", term))
        for m in _NUMBER_RE.finditer(_digits_form(text)):
            n = m.group(0)
            if len(re.sub(r"\D", "", n)) >= _MIN_NUMBER_DIGITS and n not in ctx.own_digits:
                out.append(Violation("ungrounded_number", n))
        for m in _HEX_RE.finditer(text):
            if m.group(0).lower() not in ctx.own_lower:
                out.append(Violation("ungrounded_term", m.group(0)))
    text_norm = _norm(text)
    for subject, owners in ctx.foreign_owners.items():
        if len(subject) > len(text_norm):  # 短い側 (text) より長い主題は含まれ得ない
            continue
        if subject in text_norm and ctx.situation_id not in owners and subject not in own_norm:
            out.append(Violation("foreign_subject", subject))
    wording = internal_claim_text(text)
    if wording:
        out.append(Violation("internal_wording", wording))
    return list(dict.fromkeys(out))


def _short(token: str) -> str:
    return token[:_LOG_TOKEN_CHARS]


Record = tuple[str, str, str]  # (欄, 違反の型, 語)


def _filter_items(
    items: tuple[str, ...],
    field_name: str,
    ctx: GateContext,
    mode: Mode,
    prev: tuple[str, ...],
    records: list[Record],
) -> tuple[str, ...]:
    kept: list[str] = []
    for item in items:
        violations = find_violations(item, ctx)
        records.extend((field_name, v.kind, _short(v.token)) for v in violations)
        if not violations:
            kept.append(item)
    deduped = normalized_dedupe(kept)
    if len(deduped) != len(kept):
        records.append((field_name, "duplicate", str(len(kept) - len(deduped))))
    if mode != "1":
        return items
    return deduped or (prev if items else ())


def _gate_implication(
    implication: str,
    ctx: GateContext,
    impl: Mode,
    jp: Mode,
    prev: str,
    records: list[Record],
    jp_records: list[Record],
) -> str:
    sentences = split_sentences(implication)
    kept: list[str] = []
    for sentence in sentences:
        dropped = False
        if impl != "0":
            violations = find_violations(sentence, ctx, check_terms=True)
            records.extend(("implication", v.kind, _short(v.token)) for v in violations)
            dropped = bool(violations) and impl == "1"
        if jp != "0" and not ctx.has_japan_link:
            m = JP_IMPLICATION_RE.search(sentence)
            if m:
                jp_records.append(("implication", "jp_implication", _short(m.group(0))))
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


def _log_records(event: str, sid: str, mode: str, records: list[Record]) -> None:
    """1 版につき 1 行 (発火したときだけ)。語は先頭の数件。"""
    if not records:
        return
    counts: dict[str, int] = {}
    for f, kind, _ in records:
        counts[f"{f}:{kind}"] = counts.get(f"{f}:{kind}", 0) + 1
    _log.warning(
        event,
        situation=sid,
        mode=mode,
        counts=counts,
        tokens=[f"{f}:{kind}:{tok}" for f, kind, tok in records[:_LOG_MAX_TOKENS]],
    )


def apply_revision_gates(
    analysis: ClaimAnalysis, ctx: GateContext, prev: RevisionRow | None
) -> ClaimAnalysis:
    """含意・指標・欠けている証拠に関門を掛けた新しい ClaimAnalysis (旗 0 は同じ object)。

    失敗しても台帳の run は止めない: 例外は ``ledger_revision_gate_failed`` (型名のみ) を
    残して元の analysis を返す (shadow・1 とも)。
    """
    impl, jp = impl_grounding_mode(), jp_implication_mode()
    if impl == "0" and jp == "0":
        return analysis
    try:
        return _apply(analysis, ctx, prev, impl, jp)
    except Exception as e:  # noqa: BLE001 — 関門の不具合で台帳の更新を落とさない
        _log.warning(
            "ledger_revision_gate_failed", situation=ctx.situation_id, error=type(e).__name__
        )
        return analysis


def _apply(
    analysis: ClaimAnalysis, ctx: GateContext, prev: RevisionRow | None, impl: Mode, jp: Mode
) -> ClaimAnalysis:
    records: list[Record] = []
    jp_records: list[Record] = []
    implication = _gate_implication(
        analysis.implication, ctx, impl, jp, prev.implication if prev else "", records, jp_records
    )
    indicators, missing = analysis.indicators, analysis.missing_evidence
    if impl != "0":
        indicators = _filter_items(
            indicators, "indicators", ctx, impl, _prev_list(prev, "indicators_json"), records
        )
        missing = _filter_items(
            missing, "missing_evidence", ctx, impl, _prev_list(prev, "missing_json"), records
        )
    _log_records("ledger_impl_ungrounded", ctx.situation_id, impl, records)
    _log_records("ledger_jp_implication_shadow", ctx.situation_id, jp, jp_records)
    return replace(
        analysis, implication=implication, indicators=indicators, missing_evidence=missing
    )


def no_japan_link(
    *, claim: str, anchors: Iterable[str], entity_keys: Iterable[str], evidence_text: str
) -> bool:
    """プロンプトに日本接点なしの注意を足すか (``LEDGER_JP_IMPLICATION=1`` のときだけ)。

    判定に失敗したら False (注意を足さない = 従来どおり) で、増分更新は止めない。
    """
    if jp_implication_mode() != "1":
        return False
    try:
        return not has_japan_link(
            claim=claim, anchors=anchors, entity_keys=entity_keys, evidence_text=evidence_text
        )
    except Exception as e:  # noqa: BLE001
        _log.warning("ledger_revision_gate_failed", step="no_japan_link", error=type(e).__name__)
        return False


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
    article_cache: dict[str, dict[str, Any] | None] | None = None,
) -> GateContext:
    """束ねた根拠 (見出し・要約・本文) + 既存の抜粋 + claim + anchor から照合の材料を作る。

    ``repo`` は ``get_article_grounding``、``store`` は ``evidence_excerpts`` を持つ
    (RunHistoryRepository / SituationStore)。取れない記事は飛ばす (材料が減る = 違反側に倒れる)。
    ``article_cache`` を run 内で共有すると、同じ記事を複数の状況が読むときの再取得を避けられる。
    """
    cache = article_cache if article_cache is not None else {}
    aids = list(read_aids)
    parts: list[str] = [claim]
    titles: list[str] = []
    for aid in aids:
        if aid not in cache:
            cache[aid] = repo.get_article_grounding(aid)
        g = cache[aid]
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


def build_gate_owners(situations: Iterable[SituationRow]) -> dict[str, frozenset[str]]:
    """主題の索引 (旗 0 では作らない)。失敗したら空 (= 他状況の主題の検査だけ効かない)。"""
    if impl_grounding_mode() == "0":
        return {}
    try:
        return subject_owner_index(situations)
    except Exception as e:  # noqa: BLE001
        _log.warning("ledger_revision_gate_failed", step="owners", error=type(e).__name__)
        return {}


def gate_analysis(
    analysis: ClaimAnalysis,
    *,
    prev: RevisionRow | None,
    repo: _ArticleRepo,
    store: _ExcerptStore,
    situation_id: str | None,
    row: SituationRow | None,
    claim: str,
    read_aids: Iterable[str],
    entities_by_id: Mapping[str, set[str]],
    owners: Mapping[str, frozenset[str]],
    article_cache: dict[str, dict[str, Any] | None],
) -> ClaimAnalysis:
    """旗に従い文脈の組み立てから関門までを 1 回で行う。どこで失敗しても元の analysis を返す。"""
    if impl_grounding_mode() == "0" and jp_implication_mode() == "0":
        return analysis
    try:
        ctx = build_gate_context(
            repo=repo,
            store=store,
            situation_id=situation_id,
            row=row,
            claim=claim,
            read_aids=read_aids,
            entities_by_id=entities_by_id,
            owners=owners,
            article_cache=article_cache,
        )
    except Exception as e:  # noqa: BLE001
        _log.warning(
            "ledger_revision_gate_failed",
            situation=situation_id or "",
            step="context",
            error=type(e).__name__,
        )
        return analysis
    return apply_revision_gates(analysis, ctx, prev)
