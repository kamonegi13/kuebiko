"""事象どうしの統合 (毎時の段) — 統合と本文の再生成を **対で** 行う (2026-09-21)。

毎時の群化 (``eventnews_hourly_job``) は「1 記事 × 1 事象」しか見ないため、既にできた
事象どうしは突き合わされない (実測: seed 4,617 に対し既存への参加 146 = 3%)。
長期化する事案ほど割れる = 追跡価値が高いものほど不利だった。判定の本体は
``src/eventnews/merge.py`` (純粋関数)。ここは DB との出入りと、段としての運転だけ。

⭐ **判定は ML のみ (LLM を呼ばない)**。``pair_shadow.judge_pairs`` は対ごとに LLM を
呼ぶ設計で、全事象の候補対 (実測 59,356) では 16 時間かかり成立しない。モデルは
``llm_available=False`` の閾値を持つので、そちらで判定する。検証 (2026-09-21) は
この条件で 154 群 / 389 事象 / 最大 47 記事 (ハブなし)。記事種別 (event_kind) も
検証と同じく ``other`` 固定 — 記事の 88% が未分類で、種別による除外は当てにならない。

⚠ **ML が使えないときは統合しない**。決定論だけで統合すると一括勧告が 82 事象・
277 記事を吸い込む (実測)。

⚠ 統合先は ``current_version`` を 0 に戻す = **本文が消える**。毎時の群化は新着が
入った事象しか生成しないので、統合しただけでは二度と本文が付かない。この段は
統合の直後に ``pending_items`` を上限つきで再生成する (2026-09-02 に統合だけ適用して
45 件を本文なしにした実例。統合前より悪い状態を作って「実施した」と報告しかけた)。

停止: ``EVENTNEWS_MERGE=0``。再生成の上限: ``EVENTNEWS_MERGE_REGEN_CAP`` (既定 10 件) と
``EVENTNEWS_MERGE_REGEN_BUDGET_SECONDS`` (既定 420 秒、次の件を始める前に見る)。
⭐ 上限は「1 時間あたりの生成数」であって統合数ではない — 統合は見つかった分を
全部書く (書かないと次の時間も同じ対を採点し直すだけ)。残りは翌時間以降に
新しい事象から順に埋まる。手動の一括は ``scripts/retro_merge_events.py``。
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import numpy as np

from src.eventnews import pair_model, pair_shadow
from src.eventnews.grouping import build_join_entities
from src.eventnews.merge import (
    MergeGroup,
    candidate_pairs_by_entity,
    edge_inputs,
    frequent_entity_pairs,
    plan_merges,
)
from src.eventnews.models import ENTITY_FREQ_WINDOW_HOURS, MemberArticle
from src.eventnews.pair_features import pair_features
from src.eventnews.pair_model import PairModel
from src.eventnews.runner import _max_importance
from src.eventnews.state import compute_source_breakdown
from src.logging_config import get_logger
from src.storage.repo_eventnews import EventItemRecord
from src.storage.run_history import RunHistoryRepository
from src.ui.services.eventnews_hourly_job import (
    _entity_counts,
    _join_entities_for,
    _load_members,
    _load_vectors,
    regenerate_pending,
)

_log = get_logger(__name__)

_FLAG = "EVENTNEWS_MERGE"
_CAP_ENV = "EVENTNEWS_MERGE_REGEN_CAP"
_BUDGET_ENV = "EVENTNEWS_MERGE_REGEN_BUDGET_SECONDS"
#: 再生成の時間予算 (秒)。**次の件を始める前**に見る。件数の上限だけでは 1 件 10 分超の
#: 事象 (識別子カタログ 84k 字、2026-09-21) で段の timeout (20 分) に当たった。
#: 予算 7 分 + 走行中の 1 件 (最長 ≈ 13 分) ≤ 20 分。
DEFAULT_REGEN_BUDGET_SECONDS = 420
#: 1 時間あたりの本文再生成の上限。実測で 1 件 25-55 秒 (2026-09-22、カタログ上限と
#: 書き直しの退行抑止を入れた後)。⚠ 09-21 に 40-600 秒だったのは欠陥の症状で、
#: 直した今は時間予算 (420 秒) が先に効く。段の所要は 322-507 秒 / 上限 1,200 秒。
DEFAULT_REGEN_CAP = 10
#: 全事象を読む上限。実測 11,395 件 (2026-09-21)、増分 ≈ 100 件/日。
_ALL_ITEMS_LIMIT = 50_000
#: 統合のメンバー行に残す参加信号 (旧 retro_merge_events と同じ値 — 集計の連続性)。
JOIN_SIGNAL = "retro_merge"

__all__ = [
    "DEFAULT_REGEN_CAP",
    "EventItemRecord",
    "MergeInputs",
    "MergeOutcome",
    "MergePlan",
    "PairModel",
    "apply_merge_group",
    "approve_pairs",
    "load_merge_inputs",
    "merge_and_regenerate",
    "merge_enabled",
    "plan_for",
    "regen_budget_seconds",
    "regen_cap",
    "run_eventnews_merge_hourly",
    "singleton_rescue_enabled",
]


def merge_enabled() -> bool:
    return os.environ.get(_FLAG, "1") != "0"


def regen_cap() -> int:
    raw = os.environ.get(_CAP_ENV, str(DEFAULT_REGEN_CAP)).strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else DEFAULT_REGEN_CAP


def regen_budget_seconds() -> int:
    raw = os.environ.get(_BUDGET_ENV, str(DEFAULT_REGEN_BUDGET_SECONDS)).strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else DEFAULT_REGEN_BUDGET_SECONDS


@dataclass(frozen=True)
class MergeInputs:
    """統合判定の材料。全部 article_id / item_id で引ける形にしてある。"""

    item_of: Mapping[str, str]
    first_seen: Mapping[str, str]
    members: Mapping[str, MemberArticle]
    entities: Mapping[str, frozenset[tuple[str, str]]]
    vectors: Mapping[str, np.ndarray]
    summary_vectors: Mapping[str, np.ndarray]
    records: Mapping[str, EventItemRecord]


@dataclass(frozen=True)
class MergePlan:
    """統合の計画。``skipped`` が空でなければ統合は行わない (理由が入る)。"""

    groups: tuple[MergeGroup, ...] = ()
    inputs: MergeInputs | None = None
    pairs_scored: int = 0
    pairs_approved: int = 0
    skipped: str = ""


@dataclass(frozen=True)
class MergeOutcome:
    """段の戻り値 (run_history に載せる要約)。"""

    plan: MergePlan
    applied_groups: int = 0
    absorbed_items: int = 0
    regenerated: int = 0
    regen_pending: int = 0
    elapsed_seconds: float = 0.0
    extra: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "items": len(self.plan.inputs.records) if self.plan.inputs else 0,
            "pairs_scored": self.plan.pairs_scored,
            "pairs_approved": self.plan.pairs_approved,
            "groups": len(self.plan.groups),
            "applied_groups": self.applied_groups,
            "absorbed_items": self.absorbed_items,
            "regenerated": self.regenerated,
            "regen_pending": self.regen_pending,
            "elapsed_seconds": self.elapsed_seconds,
        }
        if self.plan.skipped:
            out["skipped"] = self.plan.skipped
        return {**out, **self.extra}


def load_merge_inputs(repo: RunHistoryRepository) -> MergeInputs:
    """生きている全事象とそのメンバーを、毎時ジョブと **同じ loader** で読む。

    取得経路を分けると挙動が一致しない (2026-08-24 の不発の原因)。埋込は本文の
    埋込 (article_embeddings) を辺の判定に、要約埋込 (summary_embeddings) を ML の
    特徴に使う — 毎時の ``judge_pairs`` と同じ組合せ。
    """
    records = [
        r for r in repo.list_event_items(origin="live", limit=_ALL_ITEMS_LIMIT) if not r.merged_into
    ]
    item_of: dict[str, str] = {}
    first_seen: dict[str, str] = {}
    by_id: dict[str, EventItemRecord] = {}
    for r in records:
        by_id[r.state.item_id] = r
        first_seen[r.state.item_id] = str(r.state.first_reported_at)
        for aid in r.state.member_ids:
            item_of[aid] = r.state.item_id
    counts = _entity_counts(repo, datetime.now(UTC) - timedelta(hours=ENTITY_FREQ_WINDOW_HOURS))
    members = _load_members(repo, list(item_of), counts)
    # まとめ記事は統合の候補の組を作らない (2026-10-02)。まとめ記事を橋に別の事象どうしが
    # 統合されるのを防ぐ (群化側では辺の相手にしない、と同じ規則)
    entities = {
        aid: ents
        for aid, ents in build_join_entities(edge_inputs(members), counts).items()
        if not (aid in members and members[aid].is_roundup)
    }
    vectors: dict[str, np.ndarray] = {}
    for aid, v in _load_vectors(repo, list(members)).items():
        n = float(np.linalg.norm(v))
        if n:
            vectors[aid] = v / n
    return MergeInputs(
        item_of=item_of,
        first_seen=first_seen,
        members=members,
        entities=entities,
        vectors=vectors,
        summary_vectors=repo.load_summary_embeddings(list(members)),
        records=by_id,
    )


def approve_pairs(
    inputs: MergeInputs,
    model: PairModel,
    *,
    on_pair: Callable[[tuple[str, str]], None] | None = None,
    single_edge_ok: set[tuple[str, str]] | None = None,
    pairs: Sequence[tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    """別事象どうしの候補対を ML だけで採点し、承認した対を返す (LLM は呼ばない)。

    ``single_edge_ok`` を渡すと、承認した対のうち **まとめ系の特徴が立たない** ものを集める
    (記事 1 件の事象の救済に使う。盲検の誤り 3/3 がまとめ・日次ダイジェストだった、2026-09-27)。
    """
    if pairs is None:
        pairs = sorted(candidate_pairs_by_entity(inputs.entities, vectors=inputs.vectors))
    approved: list[tuple[str, str]] = []
    for a, b in pairs:
        ia, ib = inputs.item_of.get(a), inputs.item_of.get(b)
        if ia is None or ib is None or ia == ib:
            continue
        if on_pair:
            on_pair((a, b))
        left = pair_shadow.build_side(
            inputs.members[a], inputs.vectors[a], inputs.summary_vectors.get(a)
        )
        right = pair_shadow.build_side(
            inputs.members[b], inputs.vectors[b], inputs.summary_vectors.get(b)
        )
        # LLM 判定なし → (llm_same=0, llm_known=0) で埋め、llm_off 側の閾値で判定する
        base = pair_features(left, right)
        features = base + [0.0, 0.0]
        if model.joins(features, llm_available=False):
            approved.append((a, b))
            if single_edge_ok is not None and not _is_roundup_pair(base):
                single_edge_ok.add((a, b))
    return approved


# まとめ系の特徴 (pair_features の名前)。1 本での救済からは外す
_ROUNDUP_FEATURES: tuple[str, ...] = (
    "roundup_one",
    "roundup_both",
    "count_one",
    "count_both",
    "kind_roundup_one",
)


def _is_roundup_pair(features: list[float]) -> bool:
    from src.eventnews.pair_features import FEATURE_NAMES

    return any(features[FEATURE_NAMES.index(n)] for n in _ROUNDUP_FEATURES if n in FEATURE_NAMES)


def singleton_rescue_enabled() -> bool:
    """記事 1 件の事象の救済 (既定 ON)。

    EVENTNEWS_SINGLETON_RESCUE=0 で従来の本数の規則だけに戻す。
    """
    return os.environ.get("EVENTNEWS_SINGLETON_RESCUE", "1") != "0"


#: 頻出の名前の共有を候補にする本文の近さの下限 (2026-10-02 の測定: 0.80 で 14 日 34 組承認・
#: 目視 30 組で誤り 0。0.85 は誤りが変わらず候補が 1/3 になるだけ)
FREQUENT_ENTITY_MIN_COS = 0.80


def frequent_merge_enabled() -> bool:
    """頻出の名前の共有を統合の候補にする (既定 ON)。EVENTNEWS_MERGE_FREQUENT=0 で止める。"""
    return os.environ.get("EVENTNEWS_MERGE_FREQUENT", "1") != "0"


def _frequent_entity_edges(
    repo: RunHistoryRepository,
    inputs: MergeInputs,
    model: PairModel,
    *,
    on_pair: Callable[[tuple[str, str]], None] | None = None,
) -> list[tuple[str, str]]:
    """頻出ガードで外した名前を共有する別事象の組を、ML で採点して承認分を返す (2026-10-02)。

    大きく報じられた出来事ほど、当局や大手取引所の名前が窓内で頻出して結合信号から外れ、
    事象が割れていた。本文が十分近い組だけを候補にし、判定は通常の組と同じ ML に委ねる。
    """
    if not frequent_merge_enabled():
        return []
    uncapped = _join_entities_for(repo, list(inputs.members), {})
    pairs = frequent_entity_pairs(
        uncapped=uncapped,
        capped=inputs.entities,
        item_of=inputs.item_of,
        vectors=inputs.vectors,
        roundups={aid for aid, m in inputs.members.items() if m.is_roundup},
        threshold=FREQUENT_ENTITY_MIN_COS,
    )
    approved = approve_pairs(inputs, model, on_pair=on_pair, pairs=sorted(pairs))
    if pairs:
        _log.info("eventnews_merge_frequent", candidates=len(pairs), approved=len(approved))
    return approved


def plan_for(repo: RunHistoryRepository) -> MergePlan:
    """統合の計画を立てる (書き込みなし)。ML が使えなければ空の計画を返す。"""
    model = pair_model.load_model() if pair_shadow.is_ml_ready() else None
    if model is None:
        _log.warning("eventnews_merge_ml_not_ready")
        return MergePlan(skipped="ml_not_ready")
    inputs = load_merge_inputs(repo)
    scored = 0

    def _count(_: tuple[str, str]) -> None:
        nonlocal scored
        scored += 1

    strong: set[tuple[str, str]] | None = set() if singleton_rescue_enabled() else None
    approved = approve_pairs(inputs, model, on_pair=_count, single_edge_ok=strong)
    extra = _frequent_entity_edges(repo, inputs, model, on_pair=_count)
    groups = plan_merges(
        item_of=inputs.item_of,
        first_seen=inputs.first_seen,
        entities=inputs.entities,
        vectors=inputs.vectors,
        approved=approved,
        single_edge_ok=strong,
        extra_edges=extra,
    )
    return MergePlan(
        groups=tuple(groups),
        inputs=inputs,
        pairs_scored=scored,
        pairs_approved=len(approved),
    )


def apply_merge_group(
    repo: RunHistoryRepository,
    group: MergeGroup,
    inputs: MergeInputs,
    now: datetime,
) -> int:
    """1 群を書き込む。統合先は最初に立った事象 (URL がそこに残る)。戻り値は吸収した数。

    統合先の ``current_version`` を 0 に戻す = 本文が消える。**必ず再生成と対で呼ぶ**。
    """
    ordered = (group.target, *group.absorbed)
    records = [inputs.records[i] for i in ordered]
    all_articles = [a for r in records for a in r.state.member_ids]
    for aid in all_articles:
        repo.add_event_member(
            item_id=group.target,
            article_id=aid,
            joined_at=now,
            contributed_new_facts=0,
            join_signal=JOIN_SIGNAL,
        )
    breakdown = compute_source_breakdown(
        [inputs.members[a] for a in all_articles if a in inputs.members]
    )
    importance = ""
    for r in records:
        importance = _max_importance(importance, r.state.importance)
    repo.update_event_item(
        group.target,
        {
            "current_version": 0,  # 版を作り直させる (メンバーが変わったので本文が古い)
            "importance": importance,
            "best_source_tier": breakdown.best_tier,
            "independent_sources": breakdown.independent,
            "state_media_count": breakdown.state_media,
            "unclassified_sources": breakdown.unclassified,
            "last_reported_at": max(r.state.last_reported_at for r in records),
            "updated_at": now,
        },
    )
    for iid in group.absorbed:
        repo.update_event_item(iid, {"merged_into": group.target, "updated_at": now})
    return len(group.absorbed)


async def merge_and_regenerate(
    repo: RunHistoryRepository,
    *,
    apply: bool,
    regen_limit: int | None,
    regen_budget_seconds: float | None = None,
    on_regen_progress: Callable[[int, int, str], None] | None = None,
) -> MergeOutcome:
    """統合 (apply=True のとき) と本文再生成を **1 回の呼出で** 行う唯一の口。

    毎時の段と手動スクリプトの両方がここを通る。統合だけを呼べる口を作らない
    (統合だけ適用して本文なし、を構造で防ぐ)。``apply=False`` は dry-run で、
    再生成も行わない。
    """
    started = time.monotonic()
    plan = plan_for(repo)
    if not apply:
        return MergeOutcome(plan=plan, elapsed_seconds=round(time.monotonic() - started, 1))

    applied = absorbed = 0
    if plan.inputs is not None:
        for group in plan.groups:
            absorbed += apply_merge_group(repo, group, plan.inputs, datetime.now(UTC))
            applied += 1
    if applied:
        _log.info("eventnews_merge_applied", groups=applied, absorbed_items=absorbed)

    regen_started = time.monotonic()
    stop_when = (
        (lambda: time.monotonic() - regen_started >= regen_budget_seconds)
        if regen_budget_seconds is not None
        else None
    )
    stats, pending_total = await regenerate_pending(
        repo, limit=regen_limit, on_progress=on_regen_progress, stop_when=stop_when
    )
    return MergeOutcome(
        plan=plan,
        applied_groups=applied,
        absorbed_items=absorbed,
        regenerated=stats.generated,
        regen_pending=max(0, pending_total - stats.attempted),
        elapsed_seconds=round(time.monotonic() - started, 1),
        extra={"regen_failed": stats.failed, "regen_skipped": stats.skipped},
    )


async def run_eventnews_merge_hourly() -> dict[str, object]:
    """毎時の段の入口。戻り値は run_history に載せる要約。"""
    if not merge_enabled():
        _log.info("eventnews_merge_disabled")
        return {"skipped": "flag_off"}
    outcome = await merge_and_regenerate(
        RunHistoryRepository(),
        apply=True,
        regen_limit=regen_cap(),
        regen_budget_seconds=regen_budget_seconds(),
    )
    # ⚠ 統合数と再生成数を **同じ行に** 出す。桁で食い違っていたら本文なしが溜まっている。
    _log.info("eventnews_merge_summary", **outcome.as_dict())
    return outcome.as_dict()
