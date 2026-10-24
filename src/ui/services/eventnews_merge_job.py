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
⚠ 10-22: 種別は今は 91% に付いており、学習時の入力 (種別・要約埋込が実値) と食い違う。
``EVENTNEWS_MERGE_REAL_KIND=1`` で種別を DB から読み、要約埋込の欠けを毎時少しずつ埋める
(既定 OFF)。適用した統合は ``event_merge_decisions`` に記録し、戻す操作は
``eventnews_merge_undo.undo_merge`` 1 か所。

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
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import numpy as np

from src.cti.roundup_detector import report_period
from src.eventnews import pair_model, pair_shadow
from src.eventnews.grouping import build_join_entities
from src.eventnews.merge import (
    MIN_EDGES_BETWEEN_ITEMS,
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
from src.ui.services.eventnews_focus_job import enrich_decision_rows, run_focus_record
from src.ui.services.eventnews_hourly_job import (
    _embed_summaries,
    _entity_counts,
    _join_entities_for,
    _load_members,
    _load_vectors,
    regenerate_pending,
)
from src.ui.services.eventnews_merge_decisions import RUN_KIND_HOURLY, build_decision_rows

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
#: 統合の対にする記事対の時間差の上限 (日)。0 で無制限。
#: ⭐ 09-21 に「判定が十分厳しいので不要」として外した (全期間の実測: 30 日の上限と無制限の差は
#:   畳む事象 1,015 対 1,038 = 2%)。だが判定が緩む方向 (ML のずれ・ハブへの吸収) では時間の
#:   安全弁が無く、数か月前の事象まで吸収しうる (2026-10-22 の全体見直し)。30 日だと 2% を失うので、
#:   正しい統合をほぼ失わず被害の上限になる 60 日にした (旧 7 日は 599/1,038 で狭すぎた)。
DEFAULT_MAX_GAP_DAYS = 60
_MAX_GAP_ENV = "EVENTNEWS_MERGE_MAX_GAP_DAYS"
#: 1 時間あたりに作る要約埋込の上限 (1 件 ≈ 0.1 秒)。未作成は全メンバーの約半数 (3,500 件規模) で、
#: 数時間かけて埋まる。上限を超えた分は翌時間以降に持ち越す。
DEFAULT_EMBED_FILL_CAP = 500
_EMBED_FILL_CAP_ENV = "EVENTNEWS_MERGE_EMBED_FILL_CAP"
_REAL_KIND_ENV = "EVENTNEWS_MERGE_REAL_KIND"
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
    "embed_fill_cap",
    "fill_summary_embeddings",
    "load_merge_inputs",
    "max_gap",
    "merge_flag_snapshot",
    "real_kind_enabled",
    "record_decisions",
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
    #: 採点した記事対の ML 確率 (判断記録用。キーは (小, 大) の順)
    probas: Mapping[tuple[str, str], float] = field(default_factory=dict)
    #: 頻出の名前の共有で承認した記事対 (判断記録で出どころを分ける)
    frequent_pairs: frozenset[tuple[str, str]] = frozenset()


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
    real_kind: bool | None = None,
    probas_out: dict[tuple[str, str], float] | None = None,
) -> list[tuple[str, str]]:
    """別事象どうしの候補対を ML だけで採点し、承認した対を返す (LLM は呼ばない)。

    ``single_edge_ok`` を渡すと、承認した対のうち **まとめ系の特徴が立たない** ものを集める
    (記事 1 件の事象の救済に使う。盲検の誤り 3/3 がまとめ・日次ダイジェストだった、2026-09-27)。

    ``real_kind`` が真なら記事の種別を DB の値で渡す (未分類は "other")。偽 (既定) は全件 "other"
    (09-21 の検証時の形)。None なら ``EVENTNEWS_MERGE_REAL_KIND`` に従う。``probas_out`` を渡すと
    採点した全対の ML 確率を (小, 大) のキーで書き込む (判断記録用)。
    """
    use_real_kind = real_kind_enabled() if real_kind is None else real_kind
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
            inputs.members[a],
            inputs.vectors[a],
            inputs.summary_vectors.get(a),
            _kind_of(inputs.members[a], use_real_kind),
        )
        right = pair_shadow.build_side(
            inputs.members[b],
            inputs.vectors[b],
            inputs.summary_vectors.get(b),
            _kind_of(inputs.members[b], use_real_kind),
        )
        # LLM 判定なし → (llm_same=0, llm_known=0) で埋め、llm_off 側の閾値で判定する
        base = pair_features(left, right)
        features = base + [0.0, 0.0]
        if probas_out is not None:
            probas_out[(a, b) if a <= b else (b, a)] = model.probability(features)
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


def cve_gate_enabled() -> bool:
    """主題の CVE が食い違う事象どうしを統合しない (既定 OFF、EVENTNEWS_MERGE_CVE_GATE=1)。"""
    return os.environ.get("EVENTNEWS_MERGE_CVE_GATE", "0") == "1"


def bulletin_bridge_gate_enabled() -> bool:
    """一括の勧告を CVE 結合の橋にしない (既定 OFF、EVENTNEWS_MERGE_BULLETIN_BRIDGE_GATE=1)。"""
    return os.environ.get("EVENTNEWS_MERGE_BULLETIN_BRIDGE_GATE", "0") == "1"


def hub_gate_enabled() -> bool:
    """ハブ事象の主題でない CVE を橋にしない (EVENTNEWS_MERGE_HUB_GATE=1)。"""
    return os.environ.get("EVENTNEWS_MERGE_HUB_GATE", "0") == "1"


def report_period_gate_enabled() -> bool:
    """月だけが違う月次レポートどうしを統合しない (EVENTNEWS_MERGE_REPORT_PERIOD_GATE=1)。"""
    return os.environ.get("EVENTNEWS_MERGE_REPORT_PERIOD_GATE", "0") == "1"


def _report_periods(inputs: MergeInputs) -> dict[str, str] | None:
    if not report_period_gate_enabled():
        return None
    periods = {aid: p for aid, m in inputs.members.items() if (p := report_period(m.title))}
    return periods or None


def frequent_merge_enabled() -> bool:
    """頻出の名前の共有を統合の候補にする (既定 ON)。EVENTNEWS_MERGE_FREQUENT=0 で止める。"""
    return os.environ.get("EVENTNEWS_MERGE_FREQUENT", "1") != "0"


def real_kind_enabled() -> bool:
    """統合の ML 特徴に種別を実値で渡し、要約埋込の欠けを埋める (EVENTNEWS_MERGE_REAL_KIND=1)。

    既定 OFF = 09-21 の検証時の形 (種別は全件 "other"・欠けた要約埋込は本文 cos で代用)。
    pair ML の学習時は種別も要約埋込も実値だったので、戻すと学習時の分布に近づく。
    """
    return os.environ.get(_REAL_KIND_ENV, "0") == "1"


def _kind_of(member: MemberArticle, use_real_kind: bool) -> str:
    """ML に渡す種別。未分類・無効は学習時の退避先 "other"。"""
    if not use_real_kind:
        return "other"
    return member.kind or "other"


def max_gap() -> timedelta | None:
    """統合の辺に許す記事対の時間差の上限。0 は無制限 (None)、不正な値は既定。"""
    raw = os.environ.get(_MAX_GAP_ENV, str(DEFAULT_MAX_GAP_DAYS)).strip()
    days = int(raw) if raw.isdigit() else DEFAULT_MAX_GAP_DAYS
    return timedelta(days=days) if days > 0 else None


def embed_fill_cap() -> int:
    raw = os.environ.get(_EMBED_FILL_CAP_ENV, str(DEFAULT_EMBED_FILL_CAP)).strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else DEFAULT_EMBED_FILL_CAP


def merge_flag_snapshot() -> dict[str, object]:
    """この回の統合の判定を左右した設定 (判断記録の ``flags_json`` に残す)。"""
    gap = max_gap()
    return {
        "cve_gate": cve_gate_enabled(),
        "bulletin_bridge_gate": bulletin_bridge_gate_enabled(),
        "hub_gate": hub_gate_enabled(),
        "report_period_gate": report_period_gate_enabled(),
        "frequent": frequent_merge_enabled(),
        "singleton_rescue": singleton_rescue_enabled(),
        "real_kind": real_kind_enabled(),
        "max_gap_days": None if gap is None else gap.days,
        "min_edges": MIN_EDGES_BETWEEN_ITEMS,
    }


EmbedFn = Callable[..., Awaitable[Mapping[str, np.ndarray]]]


async def fill_summary_embeddings(
    repo: RunHistoryRepository,
    config: object,
    inputs: MergeInputs,
    *,
    cap: int,
    embed: EmbedFn = _embed_summaries,
) -> tuple[MergeInputs, int]:
    """要約埋込が無いメンバー記事に作る (新しい記事から ``cap`` 件まで)。戻り値は (入力, 作った数)。

    統合の ML は要約 cos を 2 番目に重要な特徴にするが、メンバーの約半数は未作成で本文 cos に
    縮退していた (学習時は全件あった)。作成は毎時の段で少しずつ行う。まとめ記事は統合の候補に
    ならないので作らない。埋込の失敗で段を止めない (入力は元のまま)。元の入力は変えない。
    """
    missing = [
        m
        for aid, m in inputs.members.items()
        if aid not in inputs.summary_vectors and not m.is_roundup
    ]
    if not missing:
        return inputs, 0
    missing.sort(key=lambda m: m.anchor_ts, reverse=True)
    try:
        fresh = await embed(config, missing[:cap], repo=repo)
    except Exception as e:  # noqa: BLE001 — 埋込基盤の不調で統合を止めない
        _log.warning("eventnews_merge_embed_fill_failed", error=str(e)[:160])
        return inputs, 0
    created = {aid: v for aid, v in fresh.items() if aid not in inputs.summary_vectors}
    if not created:
        return inputs, 0
    _log.info(
        "eventnews_merge_embed_filled", created=len(created), remaining=len(missing) - len(created)
    )
    return replace(inputs, summary_vectors={**inputs.summary_vectors, **created}), len(created)


def _frequent_entity_edges(
    repo: RunHistoryRepository,
    inputs: MergeInputs,
    model: PairModel,
    *,
    on_pair: Callable[[tuple[str, str]], None] | None = None,
    real_kind: bool | None = None,
    probas_out: dict[tuple[str, str], float] | None = None,
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
    approved = approve_pairs(
        inputs,
        model,
        on_pair=on_pair,
        pairs=sorted(pairs),
        real_kind=real_kind,
        probas_out=probas_out,
    )
    if pairs:
        _log.info("eventnews_merge_frequent", candidates=len(pairs), approved=len(approved))
    return approved


def plan_for(
    repo: RunHistoryRepository,
    inputs: MergeInputs | None = None,
    *,
    real_kind: bool | None = None,
) -> MergePlan:
    """統合の計画を立てる (書き込みなし)。ML が使えなければ空の計画を返す。

    ``inputs`` を渡すとそれを使う (要約埋込を埋めた入力など)。``real_kind`` は評価用の上書き。
    """
    model = pair_model.load_model() if pair_shadow.is_ml_ready() else None
    if model is None:
        _log.warning("eventnews_merge_ml_not_ready")
        return MergePlan(skipped="ml_not_ready")
    inputs = inputs or load_merge_inputs(repo)
    scored = 0
    probas: dict[tuple[str, str], float] = {}

    def _count(_: tuple[str, str]) -> None:
        nonlocal scored
        scored += 1

    strong: set[tuple[str, str]] | None = set() if singleton_rescue_enabled() else None
    approved = approve_pairs(
        inputs, model, on_pair=_count, single_edge_ok=strong, real_kind=real_kind, probas_out=probas
    )
    extra = _frequent_entity_edges(
        repo, inputs, model, on_pair=_count, real_kind=real_kind, probas_out=probas
    )
    groups = plan_merges(
        item_of=inputs.item_of,
        first_seen=inputs.first_seen,
        entities=inputs.entities,
        vectors=inputs.vectors,
        approved=approved,
        single_edge_ok=strong,
        extra_edges=extra,
        cve_disjoint_gate=cve_gate_enabled(),
        bulletin_bridge_gate=bulletin_bridge_gate_enabled(),
        hub_gate=hub_gate_enabled(),
        report_periods=_report_periods(inputs),
        article_ts={aid: m.anchor_ts for aid, m in inputs.members.items()},
        max_gap=max_gap(),
    )
    return MergePlan(
        groups=tuple(groups),
        inputs=inputs,
        pairs_scored=scored,
        pairs_approved=len(approved),
        probas=probas,
        frequent_pairs=frozenset((a, b) if a <= b else (b, a) for a, b in extra),
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


async def _prepare_inputs(
    repo: RunHistoryRepository, *, apply: bool
) -> tuple[MergeInputs | None, int]:
    """``EVENTNEWS_MERGE_REAL_KIND`` が ON のとき、要約埋込の欠けを埋めた入力を用意する。

    OFF・dry-run・ML 不在のときは何もしない (None = ``plan_for`` が自分で読む)。
    dry-run は埋込を作らない (Ollama を呼ぶ書き込みなので)。
    """
    if not (apply and real_kind_enabled() and pair_shadow.is_ml_ready()):
        return None, 0
    from src.config_loader import load_app_config

    inputs = load_merge_inputs(repo)
    return await fill_summary_embeddings(repo, load_app_config(), inputs, cap=embed_fill_cap())


def record_decisions(
    repo: RunHistoryRepository, plan: MergePlan, now: datetime, *, run_kind: str
) -> int:
    """適用した群の判断 (辺・確率・設定) を記録する。記録の失敗で統合は戻さないが握りつぶさない。"""
    inputs = plan.inputs
    if inputs is None or not plan.groups:
        return 0
    vectors = inputs.vectors

    def cos_of(a: str, b: str) -> float:
        va, vb = vectors.get(a), vectors.get(b)
        return float(np.dot(va, vb)) if va is not None and vb is not None else 0.0

    rows = build_decision_rows(
        plan.groups,
        probas=plan.probas,
        frequent_pairs=plan.frequent_pairs,
        cos_of=cos_of,
        flags=merge_flag_snapshot(),
        run_kind=run_kind,
    )

    def members_of(item_id: str) -> list[MemberArticle]:
        # 統合前の参加順 (inputs は適用前の状態)。焦点の照合は記録だけで判断には使わない
        record = inputs.records.get(item_id)
        ids = record.state.member_ids if record else ()
        return [inputs.members[a] for a in ids if a in inputs.members]

    rows = enrich_decision_rows(rows, members_of)
    try:
        return repo.record_merge_decisions(rows, decided_at=now)
    except Exception as e:  # noqa: BLE001 — 統合は適用済み。記録の失敗は大きく残して続行
        _log.error("eventnews_merge_decisions_record_failed", rows=len(rows), error=str(e)[:200])
        return 0


async def merge_and_regenerate(
    repo: RunHistoryRepository,
    *,
    apply: bool,
    regen_limit: int | None,
    regen_budget_seconds: float | None = None,
    on_regen_progress: Callable[[int, int, str], None] | None = None,
    run_kind: str = RUN_KIND_HOURLY,
) -> MergeOutcome:
    """統合 (apply=True のとき) と本文再生成を **1 回の呼出で** 行う唯一の口。

    毎時の段と手動スクリプトの両方がここを通る。統合だけを呼べる口を作らない
    (統合だけ適用して本文なし、を構造で防ぐ)。``apply=False`` は dry-run で、
    再生成も行わない。
    """
    started = time.monotonic()
    prepared, filled = await _prepare_inputs(repo, apply=apply)
    plan = plan_for(repo, prepared)
    if not apply:
        return MergeOutcome(plan=plan, elapsed_seconds=round(time.monotonic() - started, 1))

    applied = absorbed = 0
    if plan.inputs is not None:
        now = datetime.now(UTC)
        for group in plan.groups:
            absorbed += apply_merge_group(repo, group, plan.inputs, now)
            applied += 1
        recorded = record_decisions(repo, plan, now, run_kind=run_kind)
    else:
        recorded = 0
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
        extra={
            "regen_failed": stats.failed,
            "regen_skipped": stats.skipped,
            "decisions_recorded": recorded,
            "summary_embeddings_filled": filled,
        },
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
    # 焦点の記録 (F0)。統合の後に作り直す。記録だけで統合の判断には使わない
    return {**outcome.as_dict(), "focus": run_focus_record()}
