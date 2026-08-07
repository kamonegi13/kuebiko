"""段B: 台帳駆動の Estimate 構築 (SYNTHESIS_STATE=1 で出力源になる)。

毎期ゼロから nominate→ground する代わりに、持続台帳を更新して Estimate を得る:
1. 割当 (決定論): プール全記事 → 既存 Situation。150 cap の選定競争なし。
2. 増分 ACH: 新着証拠を得た Situation を「前回判定 + 新着のみ」で対称再評価
   (毎 run 再導出による仮説フリップ不安定の解消)。指標照合 (I&W) 込み。
3. detect-new: 未割当残余から PIR + 使命序列で新規追跡を開設 (落選理由の台帳化)。
4. adversarial 検証 → revision/証拠の永続化 → Estimate は「本 run で動いた判定」の射影。

見逃しは翌 run に未割当として再浮上し回復可能 (一発勝負の廃止 = 設計 §3)。
設計: docs/synthesis_situation_ledger_design.md。
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.assessment.assignment import (
    ArticleKeys,
    SituationKeys,
    build_article_keys,
    build_situation_keys,
    match_claim,
    match_situation,
    split_anchor_keys,
    topic_tokens,
)
from src.assessment.ledger import (
    _judgment_pir_ids,
    _pir_ids_for_articles,
    _reactivate_if_dormant,
    _revision_from_judgment,
    _sweep_lifecycle,
    compute_delta_type,
)
from src.assessment.situation_store import DeltaType, RevisionRow, SituationStore
from src.assessment.standing import (
    POSTURE_ACTIVE_JP,
    STANDING_KIND,
    ensure_standing_situations,
    harvest_standing_evidence,
    has_jp_direct_evidence,
    select_standing_reassessments,
    standing_enabled,
    standing_reserve,
)
from src.cti.nation_gazetteer import nations_in_text
from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository
from src.synthesis.grounded.clustering import anchor_entities, expand_claim_sources
from src.synthesis.grounded.estimate import (
    Estimate,
    EvidenceItem,
    HypothesisScore,
    KeyJudgment,
    final_confidence,
)
from src.synthesis.grounded.hypotheses import POSTURE_HYPOTHESES
from src.synthesis.grounded.incremental import (
    PriorJudgmentView,
    detect_new_claims,
    incremental_ground_and_score,
)
from src.synthesis.grounded.passes import ground_and_score, truncate_body
from src.tools.llm_client import LLMClient

_log = get_logger(__name__)

# 割当は決定論なのでプールを選定競争 (150) より広く取れる (設計 §3.0、安全上限)。
_STATEFUL_POOL_LIMIT = 500
# detect-new prompt に載せる残余の上限 (超過は log = no-silent-caps。翌 run に再浮上する)。
# 2026-07-07: detect は高速 26B (fast_llm) を使うため 150 → 500 に拡大 (プール上限と同値、
# 通常週の未割当を全件読める)。旧 150 は遅い 31B の 900s timeout を招いていた根治。
_DETECT_INPUT_MAX = 500
_MAX_NEW_SOURCES_PER_UPDATE = 8  # 増分 ACH で読む新着本文の上限/Situation
# 1 run で増分 ACH にかける Situation 数の上限 (Dense 31B は 1 呼出が数十〜百秒級のため予算制。
# 超過分は繰越し = 次 run が再回収する。no-silent-caps: 繰越は必ず log)。
# monthly は実測 29 分 (timeout 30 分に近接) かつ軌跡射影が主のため小さく。
_MAX_UPDATES_BY_PERIOD: dict[str, int] = {"daily": 6, "weekly": 12, "monthly": 4}
# 運用弁: backlog の強制排水用に cap を一時上書きする env (通常は未設定 = period 別既定)。
# 手動 run (docker exec) と併用する。定時 cron に恒常設定しない (timeout 予算を壊す)。
_REASSESS_CAP_ENV = "SYNTHESIS_REASSESS_CAP"
# 再評価キューが 1 Situation あたり回収する未読証拠の上限 (実際に本文を読むのは
# _MAX_NEW_SOURCES_PER_UPDATE 件。超過分は read_at NULL のままキューに残り silent drop
# しない — 新しい順なので新着が常に優先、静穏期に残余が排水される)。
_UNREAD_PER_SITUATION = 20


def select_reassessments(
    candidates: dict[str, list[str]],
    latest_rev_at: dict[str, str],
    *,
    cap: int,
) -> tuple[list[str], list[str]]:
    """増分 ACH にかける Situation を優先度順に選ぶ。返り値 = (選定 sid, 繰越 sid)。

    優先度: 新着証拠が多い順 (判定が動く見込みの proxy) → 最終判定が古い順 → sid (決定論)。
    評価されると未評価カウントが 0 に戻るため、高流量の Situation が予算枠を
    占有し続けず自然に輪番になる。
    """
    order = sorted(
        candidates,
        key=lambda sid: (-len(candidates[sid]), latest_rev_at.get(sid, ""), sid),
    )
    return order[:cap], order[cap:]


_FALLBACK_STANDING = 3  # 動いた判定ゼロの日に立てる standing 判定数

# 週次対称反証 sweep の有界化 (監査 2026-07-16): 1 回の batch call で安全に収まる上限
# (adversarial の max_tokens 4,000 ÷ ~50 tokens/review ≈ 80 の 1/3 マージン)。
_SWEEP_CAP = 24
_SWEEP_SALIENCE_TOP = 8  # salience 上位は毎週必ず反証に晒す (最重要判定の定着防止)


def _select_sweep_targets(
    candidates: list[KeyJudgment],
    *,
    week_number: int,
    cap: int = _SWEEP_CAP,
    salience_top: int = _SWEEP_SALIENCE_TOP,
) -> tuple[list[KeyJudgment], int]:
    """週次反証 sweep の対象を有界に選ぶ (決定論・stateless ローテ)。返り値=(対象, 今週見送り数)。

    salience 上位 salience_top 件は毎週固定で対象、残余は ISO 週番号によるラウンドロビンで
    数週かけて全量を一巡する。swept-at の状態列を持たずに公平な巡回を得る (台帳が
    いくら育っても 1 回の sweep コストは cap で一定)。
    """
    from src.assessment.salience import rank_judgments

    if len(candidates) <= cap:
        return list(candidates), 0
    ranked = rank_judgments(tuple(candidates))
    top = list(ranked[:salience_top])
    rest = sorted(ranked[salience_top:], key=lambda j: j.id)
    slots = max(1, cap - len(top))
    groups = -(-len(rest) // slots)  # ceil — 全量を groups 週で一巡
    picked = [j for i, j in enumerate(rest) if i % groups == week_number % groups]
    selected = top + picked[:slots]
    return selected, len(candidates) - len(selected)


def _prior_view(rev: RevisionRow, excerpts: list[dict[str, str]]) -> PriorJudgmentView:
    hyps = tuple(
        {
            "hypothesis": str(h.get("hypothesis", "")),
            "consistent": int(h.get("consistent", 0)),
            "inconsistent": int(h.get("inconsistent", 0)),
        }
        for h in json.loads(rev.hypotheses_json or "[]")
    )
    return PriorJudgmentView(
        claim=rev.claim,
        claim_type=rev.claim_type,
        leading_hypothesis=rev.leading_hypothesis,
        confidence=rev.confidence,
        hypotheses=hyps,
        indicators=tuple(json.loads(rev.indicators_json or "[]")),
        key_excerpts=tuple(excerpts),
    )


def _build_source(
    repo: RunHistoryRepository, aid: str, *, historical: bool = False
) -> dict[str, str] | None:
    """article_id → ACH 用 source dict (pipeline._build_source と同型)。"""
    from src.synthesis.grounded.chronology import article_chronology
    from src.synthesis.grounded.pipeline import _HISTORICAL_BODY_CHARS

    g = repo.get_article_grounding(aid)
    if not g or not g["text"]:
        return None
    chrono = article_chronology(
        report=g.get("created_at"),
        event_date=g.get("event_date"),
        event_date_basis=g.get("event_date_basis"),
    )
    text = (
        truncate_body(g["text"], max_chars=_HISTORICAL_BODY_CHARS)
        if historical
        else truncate_body(g["text"])
    )
    return {
        "article_id": aid,
        "feed_title": g["feed_title"],
        "feed_url": g["feed_url"],
        "text": text,
        "chronology": chrono.label,
        "historical": "1" if historical else "",
    }


def _final_delta(
    *,
    prev: RevisionRow,
    was_dormant: bool,
    leading: str,
    confidence: str,
    claim: str,
    scope_expanded: bool,
) -> DeltaType:
    """delta 分類 (コード決定論)。基本分類の no_change を escalated/claim_revised で細分。"""
    base = compute_delta_type(
        prev_leading=prev.leading_hypothesis,
        prev_confidence=prev.confidence,
        leading=leading,
        confidence=confidence,
        was_dormant=was_dormant,
    )
    if base == "no_change" and scope_expanded:
        return "escalated"
    if base == "no_change" and claim.strip() != prev.claim.strip():
        return "claim_revised"
    return base


def refresh_ledger_assignments(
    *,
    repo: RunHistoryRepository,
    store: SituationStore,
    db_path: Path,
    now: datetime | None = None,
) -> int:
    """割当のみの台帳リフレッシュ (LLM ゼロ・revision/report 生成なし)。

    staleness auto-trigger 用 (状態中心の原則: **収集イベント=軽い状態更新 (決定論)、
    評価とレポート生成=定時**)。stateful フル synthesis (5-12 LLM 呼出) を収集
    pipeline の尻尾で回すと 1800s 予算を構造的に超える (2026-07-04 実測 3 連続 timeout)。
    新着はここで証拠として台帳に載り、次の定時 run が増分 ACH で評価する。
    返り値: 新規に割当てた記事数。
    """
    from src.synthesis.generator import _build_high_importance_cross_axis, _resolve_period
    from src.synthesis.grounded.pipeline import _POOL_IMPORTANCE

    now_dt = now or datetime.now(UTC)
    now_iso = now_dt.isoformat()
    _s, _e, _label, lookback, _bw = _resolve_period(period_type="daily", now=now_dt)
    pool = _build_high_importance_cross_axis(
        lookback_hours=lookback,
        db_path=db_path,
        limit=_STATEFUL_POOL_LIMIT,
        importance_levels=_POOL_IMPORTANCE,
    )
    pool_ids = [str(a.get("article_id", "")) for a in pool if a.get("article_id")]
    entities_by_id = repo.entity_keys_for_articles(pool_ids)
    situations = store.load_situations(("active", "dormant"))
    # standing (常設情報要求) は汎用 matcher の対象外 — 国 anchor による戦線バケット
    # 吸着の再発防止 (段A、収穫は harvest_standing_evidence 専用経路)。
    sit_keys = [build_situation_keys(r) for r in situations if r.kind != STANDING_KIND]
    already = store.assigned_article_ids()
    assigned = 0
    reopened_seen: set[str] = set()
    for a in pool:
        aid = str(a.get("article_id", ""))
        if not aid or aid in already:
            continue
        art = build_article_keys(
            article_id=aid,
            title=str(a.get("title", "")),
            entity_keys=frozenset(entities_by_id.get(aid, set())),
        )
        matched = match_situation(art, sit_keys)
        if matched is None:
            continue
        sit, by = matched
        if store.record_assignment(
            situation_id=sit.row.situation_id, article_id=aid, added_at=now_iso, assigned_by=by
        ):
            assigned += 1
        # 監査 backlog 2026-07-05: dormant→active の無言反転で reopened シグナルが
        # 喪失していた (auto-trigger 軽量経路)。revision として可視化してから反転する。
        _reactivate_if_dormant(
            store=store, sit_row=sit.row, now_iso=now_iso, reopened_seen=reopened_seen
        )
        store.touch_situation(sit.row.situation_id, last_evidence_at=now_iso, status="active")
    # 常設情報要求 (段A): 開設 (冪等) + 専用収穫 R1-R3。flag off なら何もしない。
    standing_added = 0
    if standing_enabled():
        ensure_standing_situations(store=store, now_iso=now_iso)
        standing_added = harvest_standing_evidence(
            store=store, repo=repo, db_path=db_path, now_iso=now_iso, lookback_hours=lookback
        )
    dormant, closed = _sweep_lifecycle(store=store, now=now_dt)
    _log.info(
        "ledger_assignments_refreshed",
        pool=len(pool),
        assigned=assigned,
        standing=standing_added,
        dormant=dormant,
        closed=closed,
    )
    return assigned


def rebuild_relations(*, store: SituationStore, sit_keys: list[SituationKeys], now_iso: str) -> int:
    """active/dormant Situation 間の関係エッジを決定論で再構築 (共有 anchor、冪等)。

    same_actor / same_campaign = 強 anchor の共有、shared_nation = 国 2 つ以上の共有。
    時系列 (temporal_sequence) は event_date 被覆が上がるまで見送り。因果は主張しない (§3.4)。
    """
    added = 0
    for i, a in enumerate(sit_keys):
        for b in sit_keys[i + 1 :]:
            shared_strong = a.strong & b.strong
            for key in sorted(shared_strong):
                t, _, v = key.partition(":")
                rel = {"actor": "same_actor", "campaign": "same_campaign"}.get(t)
                if rel:
                    store.add_relation(
                        a_id=a.row.situation_id,
                        b_id=b.row.situation_id,
                        rel_type=rel,
                        basis=v,
                        now_iso=now_iso,
                    )
                    added += 1
            shared_nations = a.nations & b.nations
            if len(shared_nations) >= 2:
                store.add_relation(
                    a_id=a.row.situation_id,
                    b_id=b.row.situation_id,
                    rel_type="shared_nation",
                    basis="+".join(sorted(shared_nations)[:4]),
                    now_iso=now_iso,
                )
                added += 1
    return added


def _trajectory_note(revs: list[RevisionRow]) -> str:
    """期間内 revision 列 → 軌跡の決定論要約 (weekly/monthly の delta_note)。"""
    seq = [r.delta_type for r in revs if r.delta_type != "no_change"]
    if not seq:
        return f"期間内 {len(revs)} 回更新 (判定は不変)"
    return f"期間内の推移: {'→'.join(seq)} ({len(revs)} revisions)"


def _strongest_delta(revs: list[RevisionRow]) -> str:
    """期間内で最も大きい delta (weekly/monthly 判定の delta_type、決定論優先順)。"""
    order = (
        "hypothesis_flip",
        "opened",
        "escalated",
        "reopened",
        "strengthened",
        "weakened",
        "claim_revised",
        "closing",
    )
    present = {r.delta_type for r in revs}
    for d in order:
        if d in present:
            return d
    return "no_change"


def _japan_related_for_keys(claim: str, keys: set[str]) -> bool:
    """claim + entity キー集合から日本関連 (当事=party) かを判定する (決定論)。

    H4 (japan_relevance SSoT 化): 言及のみ (mentioned_country:JP) は routing が意図的に
    排除する層なので、salience のブーストにも乗せない (当事 = party のみ真)。
    """
    from src.cti.japan_relevance import japan_claim_relevance

    return japan_claim_relevance(claim, keys) == "party"


def _is_japan_related(j: KeyJudgment, entities_by_id: dict[str, set[str]]) -> bool:
    """判定が日本に関わるか (salience の日本関連フラグ、決定論)。"""
    keys = {k for e in j.evidence for k in entities_by_id.get(e.article_id, set())}
    return _japan_related_for_keys(j.claim, keys)


def _revision_to_judgment(
    sid: str, rev: RevisionRow, *, domain: str = "unclassified"
) -> KeyJudgment:
    """standing fallback 用: 保存済み revision を KeyJudgment に復元 (新規 LLM なし)。"""
    hyps = tuple(
        HypothesisScore(
            hypothesis=str(h.get("hypothesis", "")),
            consistent=int(h.get("consistent", 0)),
            inconsistent=int(h.get("inconsistent", 0)),
            verdict=str(h.get("verdict", "viable")),  # type: ignore[arg-type]
        )
        for h in json.loads(rev.hypotheses_json or "[]")
    )
    return KeyJudgment(
        id=sid,
        claim=rev.claim,
        domain=domain,
        leading_hypothesis=rev.leading_hypothesis,
        confidence=rev.confidence,  # type: ignore[arg-type]
        confidence_basis=rev.confidence_basis,
        hypotheses=hyps,
        evidence=(),
        key_assumptions=tuple(json.loads(rev.assumptions_json or "[]")),
        missing_evidence=tuple(json.loads(rev.missing_json or "[]")),
        indicators=tuple(json.loads(rev.indicators_json or "[]")),
        claim_type=rev.claim_type,
        implication=rev.implication,
    )


# 射影用の証拠再水和上限。抜粋表示は render 側で更に絞る (excerpts[:3])。
# 上限到達時も大半の Situation (数十件) は全量が入り、article_count の正直さを保つ。
_REHYDRATE_EVIDENCE_LIMIT = 200


def _rehydrate_for_projection(
    judgments: list[KeyJudgment],
    *,
    store: SituationStore,
    repo: RunHistoryRepository,
    evidence_limit: int = _REHYDRATE_EVIDENCE_LIMIT,
) -> list[KeyJudgment]:
    """standing/trajectory 判定に台帳証拠と日本関連フラグを復元する (監査 2026-07-05 P3)。

    ``_revision_to_judgment`` は evidence=() で復元するため、weekly/monthly の旗艦報告が
    article_count=0 + source_caveat「本評価は接地証拠が乏しく」という**虚偽**を出し、
    さらに japan_related が常に False で W_JP boost が weekly headline 選定で死んでいた。
    証拠は台帳に永続済み — 射影時に決定論で再水和する (LLM なし)。

    状態分離 (2026-07-16): EvidenceItem に載せるのは **評価済み** 行のみ (割当だけの行を
    「中立の証拠 (内容なし)」に化けさせない)。日本関連 / PIR 再導出の entity 幅は従来通り
    全割当記事から取り、未評価分は unassessed_count で正直に併記する。
    """
    out: list[KeyJudgment] = []
    for j in judgments:
        try:
            ev = tuple(
                EvidenceItem(
                    article_id=e["article_id"],
                    source_tier=e["source_tier"],
                    attribution_basis=e["attribution_basis"],  # type: ignore[arg-type]
                    excerpt=e["excerpt"],
                    polarity=e["polarity"],  # type: ignore[arg-type]
                )
                for e in store.evidence_items(j.id, limit=evidence_limit)
            )
            counts = store.evidence_state_counts([j.id]).get(j.id, {})
            jj = replace(
                j,
                evidence=ev,
                unassessed_count=max(0, counts.get("total", 0) - counts.get("assessed", 0)),
            )
            # entity 幅は評価済みに限定しない: 割当記事も観測として japan/PIR 導出に使う
            # (旧実装は bare 行込みの evidence 全行から取っていた — 幅を維持する)。
            all_ids = sorted(store.evidence_ids_by_situation([j.id]).get(j.id, set()))
            ents = repo.entity_keys_for_articles(all_ids) if all_ids else {}
            keys = {k for aid in all_ids for k in ents.get(aid, set())}
            jj = replace(jj, japan_related=_japan_related_for_keys(jj.claim, keys))
            # L1b: standing/trajectory 判定の pir_ids も現 entity から再導出 (非空時のみ)。
            # 開設時に空だった situation の salience が PIR 盲目にならないよう自己修復する。
            if all_ids:
                fresh_pir = _pir_ids_for_articles(all_ids, ents)
                if fresh_pir:
                    jj = replace(jj, pir_ids=fresh_pir)
        except Exception as e:  # noqa: BLE001 — 再水和失敗は素の判定で続行 (報告を止めない)
            _log.warning("evidence_rehydrate_failed", situation_id=j.id, error=str(e))
            jj = j
        out.append(jj)
    return out


def _closing_judgments(
    *,
    store: SituationStore,
    repo: RunHistoryRepository,
    start_iso: str,
    moved_ids: set[str],
) -> list[KeyJudgment]:
    """期間内に close された Situation を「収束」判定として射影する。

    監査 backlog 2026-07-05: closing revision は lifecycle sweep が生成するが、
    closed は割当/standing の母集団 (active/dormant) から外れるため、ここで
    拾わないと朝刊の「収束」が構造的に空のままになる。render 側は
    delta_type != no_change を【変化した判定】に載せるので「収束」ラベルで出る。
    """
    out: list[KeyJudgment] = []
    for r in store.load_situations(("closed",)):
        if not r.closed_at or r.closed_at < start_iso or r.situation_id in moved_ids:
            continue
        rev = store.latest_revision(r.situation_id)
        if rev is None:
            continue
        out.append(
            replace(
                _revision_to_judgment(r.situation_id, rev, domain=r.domain),
                delta_type="closing",
                pir_ids=r.pir_ids,
            )
        )
    return _rehydrate_for_projection(out, store=store, repo=repo, evidence_limit=5)


async def build_estimate_stateful(  # noqa: PLR0915 — 更新オペレーションの主経路 (段階は関数分割済)
    *,
    llm: LLMClient,
    period_type: str,
    repo: RunHistoryRepository,
    store: SituationStore,
    now: datetime | None = None,
    db_path: Path = Path("data/run_history.db"),
    kev_set: frozenset[str] | None = None,
    fast_llm: LLMClient | None = None,
) -> Estimate:
    """台帳を更新し、本 run で動いた判定の Estimate を返す (SYNTHESIS_STATE=1 の出力源)。

    ``fast_llm`` (26B) が渡されれば入力の多い triage (detect-new: 未割当を読んで新規 claim を
    選ぶ) に使う。narrative 推論 (ACH/adversarial 等) は主 ``llm`` (Dense 31B) のまま。
    None なら detect も ``llm`` を流用 (後方互換)。
    """
    from src.cti.source_basis import classify_source_tier, compute_source_basis
    from src.synthesis.generator import _build_high_importance_cross_axis, _resolve_period
    from src.synthesis.grounded.adversary import adversarial_review, apply_adversarial
    from src.synthesis.grounded.pipeline import (
        _CONTEXT_WINDOW_HOURS,
        _MAX_HISTORICAL_PER_CLAIM,
        _POOL_IMPORTANCE,
    )

    now_dt = now or datetime.now(UTC)
    now_iso = now_dt.isoformat()
    start, end, label, lookback, _bw = _resolve_period(period_type=period_type, now=now_dt)

    pool = _build_high_importance_cross_axis(
        lookback_hours=lookback,
        db_path=db_path,
        limit=_STATEFUL_POOL_LIMIT,
        importance_levels=_POOL_IMPORTANCE,
    )
    pool_ids = [str(a.get("article_id", "")) for a in pool if a.get("article_id")]
    entities_by_id = repo.entity_keys_for_articles(pool_ids)
    title_by_id = {str(a.get("article_id", "")): str(a.get("title", "")) for a in pool}

    situations = store.load_situations(("active", "dormant"))
    standing_sids = {r.situation_id for r in situations if r.kind == STANDING_KIND}
    row_by_sid = {r.situation_id: r for r in situations}
    latest_revs: dict[str, RevisionRow] = {}
    sit_keys: list[SituationKeys] = []
    for row in situations:
        rev = store.latest_revision(row.situation_id)
        if rev is not None:
            latest_revs[row.situation_id] = rev
        if row.kind == STANDING_KIND:
            continue  # standing は汎用 matcher の対象外 (収穫は専用経路。revision は評価に使う)
        sit_keys.append(build_situation_keys(row, claim_type=rev.claim_type if rev else ""))

    # ---- 1. 割当 (決定論): 新着記事 → 既存 Situation。書込は ACH 後 (rich な証拠行を優先) ----
    already = store.assigned_article_ids()
    new_by_sid: dict[str, list[str]] = {}
    assigned_by_aid: dict[str, str] = {}
    unassigned: list[dict[str, object]] = []
    for a in pool:
        aid = str(a.get("article_id", ""))
        if not aid or aid in already:
            continue
        art = build_article_keys(
            article_id=aid,
            title=title_by_id.get(aid, ""),
            entity_keys=frozenset(entities_by_id.get(aid, set())),
        )
        matched = match_situation(art, sit_keys)
        if matched is None:
            unassigned.append(a)
        else:
            sit, by = matched
            new_by_sid.setdefault(sit.row.situation_id, []).append(aid)
            assigned_by_aid[aid] = by

    # ---- 1a. standing (常設情報要求) の開設 + 専用収穫 (段A: 評価には回さない) ----
    if standing_enabled():
        ensure_standing_situations(store=store, now_iso=now_iso)
        harvest_standing_evidence(
            store=store, repo=repo, db_path=db_path, now_iso=now_iso, lookback_hours=lookback
        )

    # ---- 1b. 再評価キュー: 毎時 refresh が先に割当てた未読証拠を台帳から回収 ----
    # refresh_ledger_assignments (割当のみ・LLM ゼロ) が載せた証拠は already に入るため
    # 上の走査には現れない。read_at IS NULL (未読) を台帳自身から導出して増分 ACH に
    # 渡す。これが無いと割当が評価を永久に追い越す (2026-07-11 実測: 33/63 Situation の
    # 飢餓)。旧「最終 revision より新しい」比較は revision が立つたびに未読分を黙って
    # 脱落させていた (2026-07-16 状態分離で根治)。
    backlog = store.unread_evidence(per_situation_limit=_UNREAD_PER_SITUATION)
    for sid, aids in backlog.items():
        cur = new_by_sid.setdefault(sid, [])
        known_aids = set(cur)
        cur.extend(a for a in aids if a not in known_aids)
    # 段B: standing は event と競合させず専用の予約枠で選ぶ (cap の内数 = 総呼出不変)。
    standing_unassessed = {sid: new_by_sid.get(sid, []) for sid in standing_sids}
    if standing_sids:
        new_by_sid = {sid: v for sid, v in new_by_sid.items() if sid not in standing_sids}
    standing_sel: list[str] = []
    if standing_enabled() and standing_sids:
        standing_sel = select_standing_reassessments(
            standing_rows=[r for r in situations if r.kind == STANDING_KIND],
            unassessed=standing_unassessed,
            latest_rev_at={
                sid: latest_revs[sid].created_at for sid in standing_sids if sid in latest_revs
            },
            now_iso=now_iso,
            reserve=standing_reserve(),
        )
    # 証拠ゼロの standing は評価できない (sources 無し) — 予約枠を消費させない
    # (証拠ゼロの KP が毎 run 候補になり event 枠を空費した実測 07-13 の修正)。
    standing_batch: dict[str, list[str]] = {}
    for sid in standing_sel:
        aids = standing_unassessed.get(sid) or store.evidence_article_ids(
            sid, limit=_MAX_NEW_SOURCES_PER_UPDATE
        )
        if aids:
            standing_batch[sid] = aids

    # ---- 1c. 予算: 増分 ACH は高コストのため優先度順に cap、超過分は繰越し ----
    cap_env = os.environ.get(_REASSESS_CAP_ENV, "").strip()
    cap = (
        int(cap_env)
        if cap_env.isdigit() and int(cap_env) > 0
        else _MAX_UPDATES_BY_PERIOD.get(period_type, _MAX_UPDATES_BY_PERIOD["daily"])
    )
    # standing 予約枠は cap の内数 (event 側を縮めて総 LLM 呼出を不変に保つ)
    event_cap = max(1, cap - len(standing_batch))
    selected_sids, deferred_sids = select_reassessments(
        new_by_sid,
        {sid: r.created_at for sid, r in latest_revs.items()},
        cap=event_cap,
    )
    # 繰越 Situation の pool 由来割当は bare 証拠として先に永続化する (割当は失わない。
    # 評価は次 run が unread_evidence 経由で回収する)。backlog 由来は台帳に既存。
    for sid in deferred_sids:
        pool_aids = [a for a in new_by_sid[sid] if a in assigned_by_aid]
        for aid in pool_aids:
            store.record_assignment(
                situation_id=sid,
                article_id=aid,
                added_at=now_iso,
                assigned_by=assigned_by_aid[aid],  # type: ignore[arg-type]
            )
        if pool_aids:
            store.touch_situation(sid, last_evidence_at=now_iso, status="active")
    if deferred_sids:
        _log.warning(
            "stateful_reassess_deferred",
            cap=cap,
            selected=len(selected_sids),
            deferred=len(deferred_sids),
            backlog_situations=len(backlog),
            deferred_sids=deferred_sids[:10],
        )
    new_by_sid = {sid: new_by_sid[sid] for sid in selected_sids}
    # 段B: 選定された standing を評価対象に合流 (staleness 選定で新着ゼロなら既存証拠で問い直す)
    new_by_sid.update(standing_batch)
    if standing_batch:
        _log.info(
            "standing_reassessments_selected",
            selected=sorted(standing_batch),
            event_cap=event_cap,
        )

    # ---- 2. detect-new: 未割当残余から新規開設候補 (PIR + 使命序列 + 落選理由) ----
    try:
        from src.pir.integration import build_synthesis_pir_context, get_pir_config

        pir_context = build_synthesis_pir_context(get_pir_config().priorities)
    except Exception:  # noqa: BLE001 — PIR 不在でも detect-new は動かす (基準は使命序列のみ)
        pir_context = []
    detect_input = unassigned[:_DETECT_INPUT_MAX]
    if len(unassigned) > _DETECT_INPUT_MAX:
        _log.warning("stateful_detect_input_capped", total=len(unassigned), cap=_DETECT_INPUT_MAX)
    # dup guard は standing も含む全 active (detect-new が常設問いを event として再開設しない)
    active_titles = [r.title for r in situations if r.status == "active"]
    # detect (未割当を読んで新規 claim を選ぶ triage) は入力が多いので高速 26B を優先使用。
    # Dense 31B で 150件を読むと 900s を超えて timeout していた根治 (narrative は llm=31B のまま)。
    detected = await detect_new_claims(
        llm=fast_llm or llm,
        articles=detect_input,
        active_titles=active_titles,
        pir_context=pir_context,
        period_label=label,
    )

    period_start = now_dt - timedelta(hours=lookback)
    context_start = now_dt - timedelta(hours=_CONTEXT_WINDOW_HOURS.get(period_type, 90 * 24))

    # ---- 3. LLM 判定: (a) 増分更新 (b) 新規開設のフル接地。永続化は adversarial 後 ----
    pending: list[dict[str, Any]] = []  # {kind, sid?, claim, domain, analysis, ...}

    for sid, new_aids in sorted(new_by_sid.items()):
        row = row_by_sid[sid]
        is_standing = row.kind == STANDING_KIND
        # standing は POSTURE 固定フレームで競合仮説を立てる (event の domain 選択を使わない)
        hyp_override = POSTURE_HYPOTHESES if is_standing else None
        prev = latest_revs.get(sid)
        sources: list[dict[str, str]] = []
        tier_by_id: dict[str, str] = {}
        for aid in new_aids[:_MAX_NEW_SOURCES_PER_UPDATE]:
            src = _build_source(repo, aid)
            if src:
                sources.append(src)
                tier_by_id[aid] = classify_source_tier(src["feed_title"], src["feed_url"])
        if not sources:
            continue
        if prev is None:
            # revision の無い Situation (standing の初回評価 / event の異常系): フル接地
            analysis = await ground_and_score(
                llm=llm,
                claim=row.title,
                domain=row.domain,
                sources=sources,
                tier_by_id=tier_by_id,
                hypotheses_override=hyp_override,
            )
            pending.append(
                {
                    "kind": "opened",
                    "sid": sid,
                    "claim": row.title,
                    "domain": row.domain,
                    "analysis": analysis,
                    "new_aids": new_aids,
                    "read_aids": [s["article_id"] for s in sources],
                    "fired": (),
                    "scope_expanded": False,
                    "was_dormant": row.status == "dormant",
                    "standing": is_standing,
                }
            )
            continue
        try:
            inc = await incremental_ground_and_score(
                llm=llm,
                situation_title=row.title,
                prior=_prior_view(prev, store.evidence_excerpts(sid)),
                domain=row.domain,
                sources=sources,
                tier_by_id=tier_by_id,
                hypotheses_override=hyp_override,
            )
        except Exception as e:  # noqa: BLE001 — 1 Situation の失敗で run を止めない
            _log.warning("stateful_incremental_failed", situation=sid, error=str(e))
            continue
        pending.append(
            {
                "kind": "update",
                "sid": sid,
                "claim": inc.claim,
                "domain": row.domain,
                "analysis": inc.analysis,
                "new_aids": new_aids,
                "read_aids": [s["article_id"] for s in sources],
                "fired": inc.fired_indicators,
                "scope_expanded": inc.scope_expanded,
                "was_dormant": row.status == "dormant",
                "prev": prev,
                "standing": is_standing,
            }
        )

    opened_count = 0
    for c in detected.open:
        # 重複ガード: 既存 Situation に照合したら開設せず割当扱い (NetNut 型重複の根治)
        strong, nations = split_anchor_keys(
            frozenset().union(*(entities_by_id.get(a, set()) for a in c.article_ids))
            if c.article_ids
            else frozenset()
        )
        art_like = ArticleKeys(
            article_id="",
            title=c.claim,
            strong=strong,
            nations=frozenset(nations | nations_in_text(c.claim)),
            tokens=topic_tokens(c.claim),
        )
        dup = match_claim(art_like, sit_keys)
        if dup is not None:
            sid = dup[0].row.situation_id
            for aid in c.article_ids:
                store.record_assignment(
                    situation_id=sid, article_id=aid, added_at=now_iso, assigned_by="token"
                )
            store.touch_situation(sid, last_evidence_at=now_iso, status="active")
            store.log_detection(
                run_at=now_iso,
                article_id=c.article_ids[0],
                decision="assigned",
                reason=f"開設候補が既存に照合: {c.claim[:60]}",
                situation_id=sid,
            )
            continue
        # フル接地 (裏取りクラスタ拡張 + 過去文脈 — 初回接地にのみ歴史窓を使う)
        expanded = expand_claim_sources(
            claim_text=c.claim,
            seed_ids=c.article_ids,
            pool=pool,
            entities_by_id=entities_by_id,
            max_sources=_MAX_NEW_SOURCES_PER_UPDATE,
        )
        anchors_hist = anchor_entities(entities_by_id, tuple(expanded))
        hist_ids = (
            repo.articles_for_entity_keys(
                anchors_hist,
                since=context_start,
                before=period_start,
                limit=_MAX_HISTORICAL_PER_CLAIM,
            )
            if anchors_hist
            else []
        )
        sources = []
        tier_by_id = {}
        for aid in expanded:
            src = _build_source(repo, aid)
            if src:
                sources.append(src)
                tier_by_id[aid] = classify_source_tier(src["feed_title"], src["feed_url"])
        seen = {s["article_id"] for s in sources}
        for aid in hist_ids:
            if aid in seen:
                continue
            src = _build_source(repo, aid, historical=True)
            if src:
                sources.append(src)
                tier_by_id[aid] = classify_source_tier(src["feed_title"], src["feed_url"])
        if not sources:
            continue
        try:
            analysis = await ground_and_score(
                llm=llm, claim=c.claim, domain=c.domain, sources=sources, tier_by_id=tier_by_id
            )
        except Exception as e:  # noqa: BLE001
            _log.warning("stateful_open_ground_failed", claim=c.claim[:60], error=str(e))
            continue
        pending.append(
            {
                "kind": "opened",
                "sid": None,
                "claim": c.claim,
                "domain": c.domain,
                "analysis": analysis,
                "new_aids": list(expanded),
                "read_aids": [s["article_id"] for s in sources],
                "fired": (),
                "scope_expanded": False,
                "was_dormant": False,
            }
        )
        opened_count += 1

    # ---- 4. 確度較正 → KeyJudgment 化 → adversarial → 永続化 ----
    judgments: list[KeyJudgment] = []
    meta_by_id: dict[str, dict[str, Any]] = {}
    for p in pending:
        analysis = p["analysis"]
        sid = p["sid"]
        prior_aids = store.evidence_ids_by_situation([sid]).get(sid, set()) if sid else set()
        basis_ids = list({*prior_aids, *p["new_aids"]})
        sb = compute_source_basis(repo, basis_ids, kev_set=kev_set)
        conf, reason = final_confidence(
            analysis.llm_confidence, sb.confidence, analysis.leading_hypothesis, analysis.evidence
        )
        basis = f"ACH={analysis.llm_confidence} / source_basis={sb.confidence}"
        if reason:
            basis = f"{basis} / {reason}"
        # 段B posture cap (方向中立): H-P1 (日本 CI へ事前配置進行中) は JP 直接証拠
        # (帰属済み JP victim 観測) なしでは high にしない (設計 §4.2)。
        if (
            p.get("standing")
            and analysis.leading_hypothesis == POSTURE_ACTIVE_JP
            and conf == "high"
            and sid is not None
            and not has_jp_direct_evidence(db_path=db_path, situation_id=str(sid))
        ):
            conf = "moderate"
            basis = f"{basis} / posture_cap: JP直接証拠なしは moderate 上限"
        jid = sid or f"new-{len(judgments) + 1}"
        judgments.append(
            KeyJudgment(
                id=jid,
                claim=str(p["claim"]),
                domain=str(p["domain"]),
                leading_hypothesis=analysis.leading_hypothesis,
                confidence=conf,
                confidence_basis=basis,
                hypotheses=analysis.hypotheses,
                evidence=analysis.evidence,
                key_assumptions=analysis.key_assumptions,
                missing_evidence=analysis.missing_evidence,
                indicators=analysis.indicators,
                claim_type=analysis.claim_type,
                implication=analysis.implication,
            )
        )
        meta_by_id[jid] = p

    if judgments:
        try:
            reviews = await adversarial_review(llm=llm, judgments=tuple(judgments))
            judgments = [apply_adversarial(j, reviews.get(j.id)) for j in judgments]
        except Exception as e:  # noqa: BLE001
            _log.warning("adversarial_review_failed", error=str(e))

    revisions = flips = 0
    enriched: list[KeyJudgment] = []
    for j in judgments:
        p = meta_by_id[j.id]
        sid = p["sid"]
        if sid is None:
            pir_ids = _judgment_pir_ids(j, entities_by_id)
            row = store.open_situation(
                title=j.claim,
                domain=j.domain,
                anchors=_open_anchors(j, entities_by_id),
                pir_ids=pir_ids,
                now_iso=now_iso,
            )
            sid = row.situation_id
            store.log_detection(
                run_at=now_iso,
                article_id=j.evidence[0].article_id if j.evidence else "",
                decision="opened",
                reason=j.claim[:80],
                situation_id=sid,
            )
            delta: DeltaType = "opened"
            note = "新規開設 (detect-new)"
        else:
            row = row_by_sid[sid]
            # L1b: 開設時に pir 未タグ (取込 inline の遅延) で空だった situation を自己修復。
            # 現 entity から再導出し、非空なら判定と situation の両方を更新 (空なら既存維持)。
            pir_ids = _judgment_pir_ids(j, entities_by_id) or row.pir_ids
            if pir_ids and pir_ids != row.pir_ids:
                store.update_pir_ids(sid, pir_ids)
            prev = p.get("prev") or latest_revs.get(sid)
            if prev is None:
                delta, note = "opened", "初回判定"
            else:
                delta = _final_delta(
                    prev=prev,
                    was_dormant=bool(p["was_dormant"]),
                    leading=j.leading_hypothesis,
                    confidence=j.confidence,
                    claim=j.claim,
                    scope_expanded=bool(p["scope_expanded"]),
                )
                note = _delta_note(prev, j, delta)
            store.touch_situation(sid, last_evidence_at=now_iso, status="active")
            # 同一性追従 (P2): 評価済み claim が title と乖離したら title を進める。
            # 単発事象で開いた Situation が続報で scope 拡大しても「〜が実施された」の
            # ままにならない (claim は incremental の sanity ガード済み。id は不変)。
            if j.claim and j.claim != row.title:
                store.update_title(sid, j.claim)
                _log.info("situation_title_evolved", situation=sid, title=j.claim[:80])
        if p["fired"]:
            note = (note + " / " if note else "") + "指標発火: " + "; ".join(p["fired"])
        rev_row = _revision_from_judgment(
            j, situation_id=sid, delta_type=delta, delta_note=note, now_iso=now_iso
        )
        store.add_revision(rev_row)
        revisions += 1
        if delta == "hypothesis_flip":
            flips += 1
        # 証拠の永続化 (状態分離): ACH 引用は評価として上書き (毎時割当が先行した記事の
        # 評価を落とさない = 旧 add_evidence の冪等 skip が招いた silent 損失の根治)、
        # 引用に至らなかった割当分は観測のみ、prompt に読ませた記事は read_at で読了を刻む。
        cited = {ev.article_id for ev in j.evidence}
        for ev in j.evidence:
            store.record_assessment(
                situation_id=sid,
                article_id=ev.article_id,
                assessed_at=now_iso,
                polarity=ev.polarity,
                attribution_basis=ev.attribution_basis,
                excerpt=ev.excerpt,
                source_tier=ev.source_tier,
            )
        for aid in p["new_aids"]:
            if aid not in cited:
                store.record_assignment(
                    situation_id=sid,
                    article_id=aid,
                    added_at=now_iso,
                    assigned_by=assigned_by_aid.get(aid, "seed"),  # type: ignore[arg-type]
                )
        read_aids = [str(a) for a in p.get("read_aids", [])]
        if read_aids:
            store.mark_read(situation_id=sid, article_ids=read_aids, read_at=now_iso)
        counts = store.evidence_state_counts([sid]).get(sid, {})
        # 段C: delta を estimate の一級データとして judgment に刻む (render は射影に徹する)
        enriched.append(
            replace(
                j,
                id=sid,
                delta_type=delta,
                delta_note=note,
                fired_indicators=tuple(p["fired"]),
                pir_ids=pir_ids,
                japan_related=_is_japan_related(j, entities_by_id),
                unassessed_count=max(0, counts.get("total", 0) - counts.get("assessed", 0)),
            )
        )
    judgments = enriched

    # ---- 5. 落選/未割当の監査台帳 + lifecycle ----
    rejected_ids = set()
    for aid, reason in detected.rejected:
        rejected_ids.add(aid)
        store.log_detection(run_at=now_iso, article_id=aid, decision="rejected", reason=reason)
    un_high = 0
    opened_aids = {a for p in pending if p["sid"] is None for a in p["new_aids"]}
    for a in detect_input:
        aid = str(a.get("article_id", ""))
        if aid in rejected_ids or aid in opened_aids:
            continue
        if str(a.get("importance", "") or "") == "high":
            un_high += 1
            store.log_detection(
                run_at=now_iso,
                article_id=aid,
                decision="unassigned",
                reason="既存不適合・開設判断で未選定",
            )
    dormant, closed = _sweep_lifecycle(store=store, now=now_dt)

    # ---- 5b. 段D: 関係エッジ再構築 (決定論・冪等) + 週次 対称 red-team sweep ----
    all_rows = store.load_situations(("active", "dormant"))
    all_keys = [build_situation_keys(r) for r in all_rows]
    relations_added = rebuild_relations(store=store, sit_keys=all_keys, now_iso=now_iso)

    moved_ids = {j.id for j in judgments}
    swept_refuted = 0
    if period_type == "weekly":
        # アンカリング対策: 変化が無くても active の判定に反証を試みる (§3.5)。
        # 反証成立のみ revision を追記 (weakened/flip)。失敗しても run を止めない。
        # 監査 2026-07-16: 台帳成長 (41→93 situation) で全 active 一括の batch call が
        # adversarial の出力上限 (4,000 tokens) を超え sweep 全体が silent skip する
        # 切断バグ → salience 上位 + 週番号ラウンドロビンの有界ローテ (N=24) に変更。
        # 全量は数週で一巡し、今週の未対象は log する (no silent caps)。
        sweep_candidates = [
            _revision_to_judgment(
                r.situation_id,
                latest_revs[r.situation_id],
                domain=r.domain,
            )
            for r in all_rows
            if r.status == "active"
            and r.situation_id in latest_revs
            and r.situation_id not in moved_ids
        ]
        standing_targets, sweep_skipped = _select_sweep_targets(
            sweep_candidates, week_number=now_dt.isocalendar()[1]
        )
        if sweep_skipped:
            _log.info(
                "weekly_sweep_rotation",
                selected=len(standing_targets),
                skipped_until_next_weeks=sweep_skipped,
            )
        standing_targets = _rehydrate_for_projection(
            standing_targets, store=store, repo=repo, evidence_limit=5
        )
        if standing_targets:
            try:
                reviews = await adversarial_review(llm=llm, judgments=tuple(standing_targets))
                for j in standing_targets:
                    rev_after = apply_adversarial(j, reviews.get(j.id))
                    if not rev_after.adversarial_refuted:
                        continue
                    prev = latest_revs[j.id]
                    delta = compute_delta_type(
                        prev_leading=prev.leading_hypothesis,
                        prev_confidence=prev.confidence,
                        leading=rev_after.leading_hypothesis,
                        confidence=rev_after.confidence,
                        was_dormant=False,
                    )
                    store.add_revision(
                        _revision_from_judgment(
                            rev_after,
                            situation_id=j.id,
                            delta_type=delta if delta != "no_change" else "weakened",
                            delta_note=f"週次対称検証で反証: {rev_after.adversarial_note[:120]}",
                            now_iso=now_iso,
                        )
                    )
                    swept_refuted += 1
            except Exception as e:  # noqa: BLE001 — sweep 失敗で weekly を止めない
                _log.warning("weekly_adversarial_sweep_failed", error=str(e))

    # ---- 5c. 段D: weekly/monthly は「期間内 revision 軌跡」を判定に昇格 (期間=render軸) ----
    if period_type in ("weekly", "monthly"):
        # 上限は period_end でなく「当 run の now」: weekly は完結した前週を総括するため
        # end (週境界) は過去だが、当 run 自身が書く revision (増分 ACH / sweep) は
        # end より後の時刻で正当な軌跡構成要素。除外すべきは「当 run より後に書かれた
        # revision」(過去期間の backfill 再生成での未来混入) のみ。
        window_revs = store.revisions_since(
            start.isoformat(), until_iso=max(end.isoformat(), now_iso)
        )
        row_by_id = {r.situation_id: r for r in all_rows}
        trajectory: list[KeyJudgment] = []
        for sid, revs in sorted(window_revs.items()):
            row_t = row_by_id.get(sid)
            if row_t is None:
                continue
            latest = revs[-1]
            trajectory.append(
                replace(
                    _revision_to_judgment(sid, latest, domain=row_t.domain),
                    delta_type=_strongest_delta(revs),
                    delta_note=_trajectory_note(revs),
                    pir_ids=row_t.pir_ids,
                )
            )
        if trajectory:
            judgments = _rehydrate_for_projection(trajectory, store=store, repo=repo)
            moved_ids = {j.id for j in judgments}

    # ---- 6. Estimate: 動いた判定 + standing 上位 (段C: 継続セクションの供給源)。
    # standing は「新しい順」でなく salience 順 (最重要の継続判定を立てる)。
    from src.assessment.salience import rank_judgments

    standing_all = tuple(
        _rehydrate_for_projection(
            [
                replace(
                    _revision_to_judgment(
                        r.situation_id, latest_revs[r.situation_id], domain=r.domain
                    ),
                    delta_type="no_change",
                    pir_ids=r.pir_ids,
                )
                for r in situations
                if r.situation_id in latest_revs
                and r.status == "active"
                and r.situation_id not in moved_ids
            ],
            store=store,
            repo=repo,
        )
    )
    judgments = [*judgments, *rank_judgments(standing_all)[:_FALLBACK_STANDING]]

    # ---- 6b. 期間内に close された Situation を「収束」として射影 ----
    # (closing revision の生成は _sweep_lifecycle 側。失敗しても estimate 本体を止めない)
    try:
        judgments = [
            *judgments,
            *_closing_judgments(
                store=store, repo=repo, start_iso=start.isoformat(), moved_ids=moved_ids
            ),
        ]
    except Exception as e:  # noqa: BLE001
        _log.warning("closing_projection_failed", error=str(e))

    _log.info(
        "stateful_estimate_built",
        period_type=period_type,
        pool=len(pool),
        situations=len(situations),
        updated=sum(1 for p in pending if p["kind"] == "update"),
        reassess_backlog=len(backlog),
        reassess_deferred=len(deferred_sids),
        opened=opened_count,
        revisions=revisions,
        flips=flips,
        unassigned=len(unassigned),
        unassigned_high_logged=un_high,
        detect_rejected=len(detected.rejected),
        dormant=dormant,
        closed=closed,
        relations_added=relations_added,
        swept_refuted=swept_refuted,
    )
    # ---- 7. forecast lifecycle 同期 (監査 2026-07-05 P4): indicators の open→scored ----
    # 決定論・冪等・LLM なし。失敗しても estimate 本体を止めない (補助系)。
    try:
        from src.assessment.forecast import update_forecasts

        rows_now = store.load_situations(("active", "dormant", "closed"))
        active_now = {r.situation_id for r in rows_now if r.status == "active"}
        closed_now = {r.situation_id for r in rows_now if r.status == "closed"}
        fresh_revs = {
            sid: rev
            for sid in sorted(active_now)
            if (rev := store.latest_revision(sid)) is not None
        }
        update_forecasts(
            store=store,
            now=now or datetime.now(UTC),
            latest_indicators_by_sid={
                sid: [str(i) for i in json.loads(rev.indicators_json or "[]")]
                for sid, rev in fresh_revs.items()
            },
            fired_by_sid={j.id: list(j.fired_indicators) for j in judgments if j.fired_indicators},
            active_sids=active_now,
            closed_sids=closed_now,
        )
    except Exception as e:  # noqa: BLE001
        _log.warning("forecast_sync_failed", error=str(e))

    judgment_ids = [j.id for j in judgments]
    relations = tuple(
        (r["a_id"], r["b_id"], r["rel_type"], r["basis"]) for r in store.relations_for(judgment_ids)
    )
    return Estimate(
        period_type=period_type,
        period_start=start,
        period_end=end,
        judgments=tuple(judgments),
        model=getattr(llm, "model", ""),
        considered_count=len(pool),
        relations=relations,
    )


async def build_projection_estimate(
    *,
    period_type: str,
    repo: RunHistoryRepository,
    store: SituationStore,
    now: datetime,
) -> Estimate:
    """台帳の既存 revision から期間 Estimate を**射影のみ**で組み立てる (欠落期間の backfill 用)。

    評価 (割当 / 増分 ACH / detect-new / adversarial / lifecycle) は一切行わず、台帳にも
    書き込まない — 「報告は状態の射影」の純形。過去時刻 now で通常経路を回すと revision が
    過去 created_at で追記され順序が壊れるため、backfill はこの経路のみを使う
    (監査 2026-07-16 設計 §2)。§5c (軌跡昇格) + §6 (standing 射影) + §6b (収束) と同じ
    組み立てを until=now (当時の run 時刻相当) で行う。standing 射影は
    latest_revision_before(until) で当時の判定を復元する (situations.status は現在値=近似)。
    """
    from src.assessment.salience import rank_judgments
    from src.synthesis.generator import _resolve_period

    start, end, _label, lookback, _bw = _resolve_period(period_type=period_type, now=now)
    now_iso = now.isoformat()
    until_iso = max(end.isoformat(), now_iso)

    window_revs = store.revisions_since(start.isoformat(), until_iso=until_iso)
    rows = store.load_situations(("active", "dormant"))
    row_by_id = {r.situation_id: r for r in rows}
    judgments: list[KeyJudgment] = []
    for sid, revs in sorted(window_revs.items()):
        row_t = row_by_id.get(sid)
        if row_t is None:
            continue
        judgments.append(
            replace(
                _revision_to_judgment(sid, revs[-1], domain=row_t.domain),
                delta_type=_strongest_delta(revs),
                delta_note=_trajectory_note(revs),
                pir_ids=row_t.pir_ids,
            )
        )
    judgments = _rehydrate_for_projection(judgments, store=store, repo=repo)
    moved_ids = {j.id for j in judgments}

    standing_all: list[KeyJudgment] = []
    for r in rows:
        if r.status != "active" or r.situation_id in moved_ids:
            continue
        rev = store.latest_revision_before(r.situation_id, until_iso=until_iso)
        if rev is None:
            continue
        standing_all.append(
            replace(
                _revision_to_judgment(r.situation_id, rev, domain=r.domain),
                delta_type="no_change",
                pir_ids=r.pir_ids,
            )
        )
    standing_all = _rehydrate_for_projection(standing_all, store=store, repo=repo)
    judgments = [*judgments, *rank_judgments(tuple(standing_all))[:_FALLBACK_STANDING]]
    try:
        judgments = [
            *judgments,
            *_closing_judgments(
                store=store, repo=repo, start_iso=start.isoformat(), moved_ids=moved_ids
            ),
        ]
    except Exception as e:  # noqa: BLE001
        _log.warning("closing_projection_failed", error=str(e))

    # considered = 当時のノミネート・プール相当 (期間終端までの lookback 窓、正直な母数表示用)
    with repo._connect() as conn:  # noqa: SLF001 — 読み取り専用の意図的共有
        row_c = conn.execute(
            "SELECT COUNT(DISTINCT article_id) FROM articles"
            " WHERE status = 'posted' AND importance IN ('high', 'medium')"
            " AND created_at >= ? AND created_at <= ?",
            ((now - timedelta(hours=lookback)).isoformat(), now_iso),
        ).fetchone()
    considered = int(row_c[0] or 0)

    judgment_ids = [j.id for j in judgments]
    relations = tuple(
        (r["a_id"], r["b_id"], r["rel_type"], r["basis"]) for r in store.relations_for(judgment_ids)
    )
    _log.info(
        "projection_estimate_built",
        period_type=period_type,
        trajectory=len(moved_ids),
        judgments=len(judgments),
        considered=considered,
    )
    return Estimate(
        period_type=period_type,
        period_start=start,
        period_end=end,
        judgments=tuple(judgments),
        model="",
        considered_count=considered,
        relations=relations,
    )


def _open_anchors(j: KeyJudgment, entities_by_id: dict[str, set[str]]) -> frozenset[str]:
    """開設 Situation の anchors (強 entity + 国) を証拠記事 entity + claim gazetteer で導出。"""
    keys: set[str] = set()
    nations: set[str] = set(nations_in_text(j.claim))
    for e in j.evidence:
        strong, ns = split_anchor_keys(frozenset(entities_by_id.get(e.article_id, set())))
        keys |= strong
        nations |= ns
    keys |= {f"involved_country:{n}" for n in nations}
    return frozenset(keys)


def _delta_note(prev: RevisionRow, j: KeyJudgment, delta: DeltaType) -> str:
    if delta == "hypothesis_flip":
        return f"見立て {prev.leading_hypothesis}→{j.leading_hypothesis}"
    if delta in ("strengthened", "weakened"):
        return f"確度 {prev.confidence}→{j.confidence}"
    if delta == "claim_revised":
        return "claim 文言を改訂"
    if delta == "escalated":
        return "被害・標的の拡大を観測"
    if delta == "reopened":
        return "休眠から再活性化"
    return ""
