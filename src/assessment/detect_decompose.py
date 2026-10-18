"""detect を既存の部品へ分けた形 (M2、2026-10-16、``DETECT_DECOMPOSE`` 既定 0)。

現行: 候補 (detect ML) → LLM が「選択・束ね・claim・棄却」を 1 回で出す → 束ねの関門で割り直す。
分けた形: 候補 (detect ML) → **塊化** (事象 id・強い鍵・要約 cos、``split_clusters``) →
**割当 ML** で追跡中の情勢に合流 → 残った塊だけ LLM が「塊 → claim 1 文」を書く。
LLM は選択も束ねも棄却もせず (棄却は「追跡中と同一」「評価対象なし」の 2 つだけ)、記事 id は
塊そのものを使う (LLM が id を出さない = 引用の実在が構造的に保証される)。

モード: ``0`` = 何もしない / ``shadow`` = 本番の detect はそのまま、分けた形の結果を
``detect_bundles`` に記録するだけ / ``1`` = 分けた形の開設・合流を本番に使う。
開設の上限 (``_DETECT_OPEN_MAX_TOTAL``) と下流の関門 (重複ガード・引用の実在・主語の関門) は
現行と同じものを通す。純粋ロジック (``decompose_candidates``) と、本番の部品への接着を分ける。
"""

from __future__ import annotations

import os
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, field_validator

from src.assessment.claim_bundle import split_clusters
from src.assessment.detect_bundle_log import (
    ORIGIN_DECOMPOSE,
    BundleRecord,
    Outcome,
    Variant,
    make_claim_id,
)
from src.logging_config import get_logger
from src.synthesis.grounded.incremental import (
    _DETECT_OPEN_MAX,
    DetectedClaim,
    DetectResult,
)
from src.synthesis.grounded.passes import _render
from src.tools.reason_trim import trim_reason_logged

if TYPE_CHECKING:
    from src.assessment.assignment import ArticleKeys, SituationKeys
    from src.assessment.situation_store import SituationStore
    from src.storage.run_history import RunHistoryRepository
    from src.tools.llm_client import LLMClient

_log = get_logger(__name__)

DecomposeMode = Literal["0", "shadow", "1"]
_MODE_ENV = "DETECT_DECOMPOSE"
_TEMPLATE = "synthesis/detect_cluster_claim.j2"
#: claim の最大字数 (プロンプトの「最大 200 字」。超過は契約で落とさず後段で切り詰める)
CLAIM_MAX_CHARS = 200
#: 出力契約の claim の上限 (暴走だけを止める緩い値。200 字超で塊を失わない)
_WIRE_CLAIM_MAX_CHARS = 600
#: shadow の追加 LLM 呼出の時間の予算 (秒) と書き手の件数 (本番の開設ループの後ろで走らせる)
SHADOW_BUDGET_SECONDS = 120.0
SHADOW_MAX_CLAIMS = 6
_SKIP_REASON_MAX_CHARS = 120
#: 合流の判定: 塊の記事のうち ML が通した割合がこれを **超える** (半数ちょうどは合流しない =
#: 迷いは detect 側に残す。割当 ML の誤合流は本来開くべき追跡を消す — CLAUDE.md 実測)
MERGE_MIN_PASS_FRACTION = 0.5
#: 合流先の候補にする追跡中の情勢の上限 (採点の組数を抑える。超過は log)
MAX_MERGE_SITUATIONS = 40
_TEMPERATURE = 0.2
_MAX_TOKENS = 600
_DOMAINS = frozenset(
    {
        "political",
        "military",
        "economic",
        "social",
        "infrastructure",
        "information",
        "cyber_incident",
        "geopolitical",
        "tech",
    }
)


def decompose_mode() -> DecomposeMode:
    """未知の値は ``0`` (何もしない側) に倒す。"""
    raw = os.environ.get(_MODE_ENV, "0").strip().lower()
    if raw == "shadow":
        return "shadow"
    if raw == "1":
        return "1"
    return "0"


class ClusterClaimWire(BaseModel):
    """「塊 → claim 1 文」課題の出力契約。配列も自由な id も無い (暴走・引用の捏造を構造で防ぐ)。"""

    model_config = {"extra": "ignore"}
    claim: str = ""
    domain: str = ""
    skip_reason: str = ""

    @field_validator("claim", mode="after")
    @classmethod
    def _trim_claim(cls, v: str) -> str:
        return trim_reason_logged(v, owner="ClusterClaimWire.claim", limit=_WIRE_CLAIM_MAX_CHARS)

    @field_validator("skip_reason", mode="after")
    @classmethod
    def _trim_skip_reason(cls, v: str) -> str:
        return trim_reason_logged(
            v, owner="ClusterClaimWire.skip_reason", limit=_SKIP_REASON_MAX_CHARS
        )


@dataclass(frozen=True)
class WrittenClaim:
    claim: str
    domain: str
    skip_reason: str


@dataclass(frozen=True)
class MergeVerdict:
    """塊を合流させる先と根拠。probability は通した記事の平均 (埋込判定のみなら None)。"""

    situation_id: str
    pass_fraction: float
    probability: float | None


@dataclass(frozen=True)
class ClusterOutcome:
    cluster: tuple[str, ...]
    action: Literal["merge", "open", "skip"]
    situation_id: str | None = None
    claim: str = ""
    domain: str = ""
    reason: str = ""
    probability: float | None = None
    violations: tuple[str, ...] = ()  # claim の固有名詞で塊の見出し・要約に無いもの


@dataclass(frozen=True)
class DecomposeResult:
    outcomes: tuple[ClusterOutcome, ...]
    overflow: int  # 書き手の上限で claim を書かなかった塊の数

    def opened(self) -> tuple[ClusterOutcome, ...]:
        return tuple(o for o in self.outcomes if o.action == "open")

    def merged(self) -> tuple[ClusterOutcome, ...]:
        return tuple(o for o in self.outcomes if o.action == "merge")


MergeFn = Callable[[Sequence[str]], MergeVerdict | None]
WriteFn = Callable[[Sequence[str]], Awaitable[WrittenClaim | None]]


async def decompose_candidates(
    *,
    candidate_ids: Sequence[str],
    ml_scores: Mapping[str, float],
    strong_by_aid: Mapping[str, set[str] | frozenset[str]],
    items_by_aid: Mapping[str, set[str] | frozenset[str]],
    vec_by_aid: Mapping[str, Any] | None,
    merge_for: MergeFn,
    write_claim: WriteFn,
    max_claims: int = _DETECT_OPEN_MAX,
    grounding_text: Callable[[Sequence[str]], str] | None = None,
    enforce_grounding: bool = False,
) -> DecomposeResult:
    """候補を塊化 → 合流 → 残りの塊だけ claim を書く (本番の部品はすべて引数で受ける)。

    塊は detect ML の確率 (塊の最大) の高い順に処理し、書き手の呼出は ``max_claims`` まで。
    書き手の失敗・skip は開設しない (記事は未割当に残り次の run で候補になる)。
    ``grounding_text`` (塊の見出し・要約) があれば claim の固有名詞が載っているかを決定論で検査し、
    ``enforce_grounding`` なら違反の claim は開設しない (shadow は違反を記録するだけ)。
    """
    clusters = split_clusters(
        candidate_ids,
        strong_by_aid=strong_by_aid,
        items_by_aid=items_by_aid,
        vec_by_aid=vec_by_aid,
        bare_as_singletons=True,
    )
    ranked = sorted(clusters, key=lambda g: (-max(ml_scores.get(a, 0.0) for a in g), -len(g), g[0]))
    outcomes: list[ClusterOutcome] = []
    written = overflow = 0
    for cluster in ranked:
        ids = tuple(cluster)
        verdict = merge_for(ids)
        if verdict is not None:
            outcomes.append(
                ClusterOutcome(
                    cluster=ids,
                    action="merge",
                    situation_id=verdict.situation_id,
                    probability=verdict.probability,
                    reason=f"割当 ML が通した割合 {verdict.pass_fraction:.2f}",
                )
            )
            continue
        if written >= max_claims:
            overflow += 1
            outcomes.append(ClusterOutcome(cluster=ids, action="skip", reason="開設の上限"))
            continue
        written += 1
        text = grounding_text(ids) if grounding_text is not None else None
        outcomes.append(await _write_one(ids, write_claim, text, enforce_grounding))
    if overflow:
        _log.warning("detect_decompose_overflow", skipped=overflow, cap=max_claims)
    return DecomposeResult(outcomes=tuple(outcomes), overflow=overflow)


async def _write_one(
    ids: tuple[str, ...], write_claim: WriteFn, text: str | None, enforce_grounding: bool
) -> ClusterOutcome:
    try:
        res = await write_claim(ids)
    except Exception as e:  # noqa: BLE001 — 書けない塊は開設しない (次の run へ)
        _log.warning("detect_decompose_write_failed", error=type(e).__name__)
        return ClusterOutcome(cluster=ids, action="skip", reason="claim の生成に失敗")
    if res is None or not res.claim.strip():
        reason = res.skip_reason.strip() if res is not None else "claim なし"
        return ClusterOutcome(cluster=ids, action="skip", reason=reason or "claim なし")
    domain = res.domain.strip()
    claim = res.claim.strip()
    violations = ungrounded_terms(claim, text) if text is not None else ()
    if violations and enforce_grounding:
        return ClusterOutcome(
            cluster=ids,
            action="skip",
            reason=f"claim の固有名詞が記事に無い: {', '.join(violations)}"[:200],
            violations=violations,
        )
    return ClusterOutcome(
        cluster=ids,
        action="open",
        claim=claim,
        domain=domain if domain in _DOMAINS else "unclassified",
        violations=violations,
    )


#: 固有名詞らしい語: 英数字の語で大文字か数字を含むもの (CVE-2026-1234・LockBit・APT29 等) と、
#: 3 字以上のカタカナ語。一般語 (小文字だけの英単語) と漢字語は決定論では判定しない
_LATIN_TERM = re.compile(r"[A-Za-z][A-Za-z0-9._-]{2,}")
_KATAKANA_TERM = re.compile(r"[ァ-ヴー]{3,}")


def ungrounded_terms(claim: str, source_text: str) -> tuple[str, ...]:
    """claim の固有名詞 (上の 2 種) のうち、塊の見出し・要約に **無い** もの (出現順、重複なし)。

    ``--`` 区切りの語尾 (ピリオド・ハイフンの末尾) は除いて照合し、英字は大文字小文字を無視する。
    見出しに無い属性を LLM が足す (捏造) を決定論で止める関門。
    """
    haystack = source_text.lower()
    missing: list[str] = []
    for m in _LATIN_TERM.finditer(claim):
        term = m.group(0).rstrip("._-")
        if not any(c.isupper() or c.isdigit() for c in term):
            continue
        if term.lower() not in haystack and term not in missing:
            missing.append(term)
    for m in _KATAKANA_TERM.finditer(claim):
        term = m.group(0)
        if term not in source_text and term not in missing:
            missing.append(term)
    return tuple(missing)


def to_detect_result(result: DecomposeResult) -> DetectResult:
    """``mode=1`` で既存の開設ループへ渡す形 (引用 = 塊の記事 id そのもの)。"""
    opens = tuple(
        DetectedClaim(claim=o.claim, domain=o.domain, article_ids=o.cluster)
        for o in result.opened()
    )
    rejected = tuple(
        (a, o.reason[:200]) for o in result.outcomes if o.action == "skip" for a in o.cluster
    )
    return DetectResult(open=opens, rejected=rejected, overflow=result.overflow)


def to_records(
    result: DecomposeResult, *, run_at: str, variant: Variant, model: str = ""
) -> list[BundleRecord]:
    """``detect_bundles`` の行。開設は pending (開設ループが確定)、合流・見送りはここで確定。"""
    records: list[BundleRecord] = []
    for o in result.outcomes:
        outcome: Outcome
        if o.action == "open":
            # mode=1 は開設ループが確定する (pending)。shadow は開設しないので shadow_open
            outcome = "pending" if variant == "decompose" else "shadow_open"
        else:
            outcome = "merged" if o.action == "merge" else "skipped"
        reason = o.reason
        if outcome == "shadow_open":
            reason = f"shadow: 本番では開設しない / 固有名詞の違反 {len(o.violations)} 件"
            if o.violations:
                reason = f"{reason} ({', '.join(o.violations)})"[:200]
        records.append(
            BundleRecord(
                run_at=run_at,
                variant=variant,
                claim_id=make_claim_id(run_at, variant, o.cluster, o.claim),
                origin=ORIGIN_DECOMPOSE,
                claim=o.claim,
                domain=o.domain,
                llm_article_ids=(),
                final_article_ids=o.cluster,
                outcome=outcome,
                situation_id=o.situation_id,
                reason=reason,
                probability=o.probability,
                model=model,
            )
        )
    return records


def pick_merge(
    cluster: Sequence[str],
    scores: Mapping[tuple[str, str], tuple[bool, float | None]],
    situation_ids: Sequence[str],
) -> MergeVerdict | None:
    """塊ごとに追跡中の情勢を採点し、半数を超える記事が通した情勢のうち最良へ合流させる。

    ``scores`` = (situation_id, article_id) → (割当 ML が通したか, 確率)。順位は
    (通した割合, 通した記事の平均確率) の降順、同点は situation_id 昇順 (決定論)。
    """
    ranked: list[tuple[float, float, str]] = []
    for sid in situation_ids:
        got = [scores[(sid, a)] for a in cluster if (sid, a) in scores]
        if len(got) < len(cluster):
            continue  # 採点できていない記事がある情勢には合流させない
        passed = [p for ok, p in got if ok]
        frac = len(passed) / len(cluster)
        if frac <= MERGE_MIN_PASS_FRACTION:
            continue
        probs = [p for p in passed if p is not None]
        mean_p = sum(probs) / len(probs) if probs else -1.0
        ranked.append((-frac, -mean_p, sid))
    if not ranked:
        return None
    neg_frac, neg_p, sid = min(ranked)
    return MergeVerdict(
        situation_id=sid, pass_fraction=-neg_frac, probability=None if neg_p > 0 else -neg_p
    )


async def write_cluster_claim(
    *,
    llm: LLMClient,
    articles: list[dict[str, object]],
    active_titles: list[str],
    pir_context: list[dict[str, str]],
    period_label: str,
) -> WrittenClaim | None:
    """塊 1 つの claim を書かせる (出力は ``ClusterClaimWire``)。記事が無ければ None。"""
    if not articles:
        return None
    prompt = _render(
        _TEMPLATE,
        articles=articles,
        active_titles=active_titles,
        pir_context=pir_context,
        period_label=period_label,
    )
    r = await llm.generate_structured(
        prompt, ClusterClaimWire, temperature=_TEMPERATURE, max_tokens=_MAX_TOKENS, think=False
    )
    # 出力契約は緩く (長すぎで塊を失わない)、ここで CLAIM_MAX_CHARS に切り詰める
    return WrittenClaim(
        claim=r.claim.strip()[:CLAIM_MAX_CHARS],
        domain=r.domain,
        skip_reason=r.skip_reason.strip(),
    )


async def score_merge_pairs(
    *,
    candidate_ids: Sequence[str],
    active_titles: Sequence[str],
    situation_keys: Sequence[SituationKeys],
    art_keys: Mapping[str, ArticleKeys],
    repo: RunHistoryRepository,
    store: SituationStore,
    save_fresh: bool = True,
) -> tuple[dict[tuple[str, str], tuple[bool, float | None]], list[str]]:
    """候補 × 追跡中の情勢を割当 ML (本番の関門と同じ判定器) で採点する。

    返り値 = (組の採点, 採点した情勢の id)。追跡中の情勢は detect に渡す ``active_titles``
    (候補と意味的に近いものだけ) に限る。probe の特徴量 (gemma 呼出) は使わない = 追加の
    LLM 呼出なし。``save_fresh=False`` (shadow) は要約埋込を DB に保存しない。
    材料が無い (ML 無効・情勢なし) なら空 (合流はゼロ = 全部 claim の書き手へ)。
    """
    from src.assessment import stateful

    wanted = set(active_titles)
    sits = [k for k in situation_keys if k.row.title in wanted][:MAX_MERGE_SITUATIONS]
    aids = [a for a in candidate_ids if a in art_keys]
    if not sits or not aids:
        return {}, []
    if len([k for k in situation_keys if k.row.title in wanted]) > MAX_MERGE_SITUATIONS:
        _log.warning("detect_decompose_situations_capped", cap=MAX_MERGE_SITUATIONS)
    kept = {k.row.situation_id: list(aids) for k in sits}
    sit_vecs, art_vecs = await stateful._gate_vectors(  # noqa: SLF001
        kept, {k.row.situation_id: k.row.title for k in sits}, repo, save_fresh=save_fresh
    )
    decide, _judge = stateful._gate_decider(  # noqa: SLF001
        kept,
        sit_vecs,
        art_vecs,
        repo=repo,
        art_keys=art_keys,
        sit_keys={k.row.situation_id: k for k in sits},
        store=store,
    )
    scores = {(sid, a): decide(sid, a) for sid in kept for a in aids}
    return scores, sorted(kept)


@dataclass(frozen=True)
class DecomposeInputs:
    """``run_decompose`` の入力 (本番の部品を束ねた不変の束)。"""

    candidates: Sequence[Mapping[str, object]]
    ml_scores: Mapping[str, float]
    entities_by_id: Mapping[str, set[str]]
    situation_keys: Sequence[SituationKeys]
    art_keys: Mapping[str, ArticleKeys]
    repo: RunHistoryRepository
    store: SituationStore
    llm: LLMClient
    active_titles: list[str]
    pir_context: list[dict[str, str]]
    period_label: str


def _grounding_text(
    by_id: Mapping[str, Mapping[str, object]], summaries: Mapping[str, str], cluster: Sequence[str]
) -> str:
    """塊の見出し + 要約 (プールの記事は要約を持たないので DB から引いた分を足す)。"""
    return "\n".join(
        f"{by_id[a].get('title', '')} {summaries.get(a, '')}" for a in cluster if a in by_id
    )


async def run_decompose(
    inp: DecomposeInputs,
    *,
    merge_enabled: bool,
    persist: bool,
    enforce_grounding: bool,
    max_claims: int = _DETECT_OPEN_MAX,
) -> DecomposeResult | None:
    """本番の部品を束ねて分けた形を 1 回走らせる。ML の採点が無ければ None (従来の detect のまま)。

    候補 = 絞り込み後の候補 (detect ML 上位 + high の下限)。塊化の材料は ``_unbundle_detected``
    と同じ (事象 id・強い鍵・要約 cos)。``merge_enabled`` は割当の関門 (``ASSIGN_EMBED_GATE``) に
    従って呼び出し側が決める。``persist=False`` (shadow) は要約埋込を DB に保存しない。
    """
    from src.assessment import stateful
    from src.assessment.assignment import split_anchor_keys

    if not inp.ml_scores or not inp.candidates:
        return None
    by_id = {str(a.get("article_id", "")): dict(a) for a in inp.candidates}
    ids = [a for a in by_id if a]
    summaries = {a: str(rec.summary or "") for a, rec in inp.repo.get_articles_by_ids(ids).items()}
    scores: dict[tuple[str, str], tuple[bool, float | None]] = {}
    sids: list[str] = []
    if merge_enabled:
        scores, sids = await score_merge_pairs(
            candidate_ids=ids,
            active_titles=inp.active_titles,
            situation_keys=inp.situation_keys,
            art_keys=inp.art_keys,
            repo=inp.repo,
            store=inp.store,
            save_fresh=persist,
        )

    async def write(cluster: Sequence[str]) -> WrittenClaim | None:
        return await write_cluster_claim(
            llm=inp.llm,
            articles=[by_id[a] for a in cluster if a in by_id],
            active_titles=inp.active_titles,
            pir_context=inp.pir_context,
            period_label=inp.period_label,
        )

    return await decompose_candidates(
        candidate_ids=ids,
        ml_scores=inp.ml_scores,
        strong_by_aid={
            a: split_anchor_keys(frozenset(inp.entities_by_id.get(a, set())))[0] for a in ids
        },
        items_by_aid=inp.repo.event_item_ids_by_article(ids),
        vec_by_aid=stateful._unit_vectors(inp.repo.load_summary_embeddings(ids)),  # noqa: SLF001
        merge_for=lambda cluster: pick_merge(cluster, scores, sids),
        write_claim=write,
        max_claims=max_claims,
        grounding_text=lambda cluster: _grounding_text(by_id, summaries, cluster),
        enforce_grounding=enforce_grounding,
    )


def merged_assignments(result: DecomposeResult) -> dict[str, list[str]]:
    """``mode=1``: 合流と判定した塊を 情勢 id → 記事 id に直す (DB には書かない)。

    呼び出し側 (stateful) が通常の割当 (``new_by_sid``) に足す。同じ run の増分 ACH で評価され、
    永続化・休眠からの復帰 (reopened) は通常の割当と同じ経路で行われる。
    """
    out: dict[str, list[str]] = {}
    for o in result.merged():
        if o.situation_id is not None:
            out.setdefault(o.situation_id, []).extend(o.cluster)
    return out
