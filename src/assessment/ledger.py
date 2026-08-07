"""Situation Ledger の更新オペレーション (段A: 割当 + 開設 + revision + lifecycle)。

daily/weekly/monthly の grounded run 後に呼ばれ、Estimate の判定を持続台帳に反映する:
1. 判定 → 既存 Situation に照合 (matched=revision 追記 / unmatched=開設)。
2. プール全記事 → 既存 Situation へ決定論割当 (証拠追記)。150 cap の選定競争を経ない。
3. lifecycle (active→dormant→closed) と選定台帳 (unassigned high の監査) を維持。

LLM は呼ばない (段B で増分 ACH / 新規開設判断が乗る)。shadow 運用: 出力 (synthesis 報告) は
不変で台帳だけが育つ。失敗しても synthesis を止めない (呼出側で捕捉)。
設計: docs/synthesis_situation_ledger_design.md §3。flag: SYNTHESIS_STATE (0/shadow/1)。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from dataclasses import replace as dc_replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

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
from src.assessment.situation_store import (
    _MAX_PIR_IDS,
    DeltaType,
    RevisionRow,
    SituationRow,
    SituationStore,
)
from src.cti.nation_gazetteer import nations_in_text
from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository
from src.synthesis.grounded.estimate import Estimate, KeyJudgment, reconcile_hypotheses

_log = get_logger(__name__)

_STATE_ENV = "SYNTHESIS_STATE"
_CONF_RANK: dict[str, int] = {"low": 0, "moderate": 1, "high": 2}
# lifecycle 閾値 (§2.3)。domain 別 dormancy、閉鎖は dormancy 経過後の追加日数。
_DORMANT_AFTER_DAYS: dict[str, int] = {"cyber_incident": 14}
_DORMANT_DEFAULT_DAYS = 30
_CLOSE_AFTER_DORMANT_DAYS = 90
_DETECTION_LOG_RETENTION_DAYS = 90


def ledger_mode() -> Literal["off", "shadow", "on"]:
    """状況台帳の有効化モード (既定 off)。shadow/on とも段A では台帳更新のみ (出力不変)。"""
    v = os.environ.get(_STATE_ENV, "0").strip().lower()
    if v == "shadow":
        return "shadow"
    return "on" if v in ("1", "true", "yes", "on") else "off"


@dataclass(frozen=True)
class LedgerStats:
    """1 回の台帳更新の監査カウント (no-silent-caps: 落ちたものを必ず数える)。"""

    situations_before: int
    opened: int
    revisions: int
    evidence_added: int
    assigned_articles: int
    unassigned_high: int
    unassigned_medium: int
    dormant_marked: int
    closed_marked: int


def compute_delta_type(
    *,
    prev_leading: str | None,
    prev_confidence: str | None,
    leading: str,
    confidence: str,
    was_dormant: bool,
) -> DeltaType:
    """revision の delta を前回との決定論比較で分類 (LLM に自己申告させない §3.2)。"""
    if prev_leading is None:
        return "opened"
    if was_dormant:
        return "reopened"
    if prev_leading != leading:
        return "hypothesis_flip"
    prev_rank = _CONF_RANK.get(prev_confidence or "", 1)
    rank = _CONF_RANK.get(confidence, 1)
    if rank > prev_rank:
        return "strengthened"
    if rank < prev_rank:
        return "weakened"
    return "no_change"


def _add_closing_revision(
    *, store: SituationStore, situation_id: str, idle_days: int, now_iso: str
) -> None:
    """自動 close の判定自体を revision として残す (§2.3「低確度の活動観測されず」)。

    監査 backlog 2026-07-05: closing revision の producer がゼロで、朝刊の
    「収束」が構造的に空だった。lifecycle sweep が status を倒すだけでなく
    収束判定 (低確度・決定論) を台帳に残すことで射影可能にする。
    """
    prev = store.latest_revision(situation_id)
    if prev is None:
        return
    store.add_revision(
        dc_replace(
            prev,
            rev=0,  # add_revision が採番
            confidence="low",
            confidence_basis="新規証拠の途絶による自動収束判定",
            delta_type="closing",
            delta_note=f"約 {idle_days} 日間新規証拠なし — 活動観測されず (自動 close)",
            created_at=now_iso,
            run_id="",
        )
    )


def _reactivate_if_dormant(
    *, store: SituationStore, sit_row: SituationRow, now_iso: str, reopened_seen: set[str]
) -> None:
    """証拠割当による dormant→active 復帰を reopened revision として可視化する。

    監査 backlog 2026-07-05: 従来は touch_situation(status='active') が無言で反転し、
    次回の判定更新では was_dormant=False になるため「再浮上」シグナルが恒久喪失
    していた。revision を残すことで render の【変化した判定】に「再浮上」が載る。
    reopened_seen は同一 run 内の重複 revision 防止 (複数記事が同じ Situation に割当)。
    """
    if sit_row.status != "dormant" or sit_row.situation_id in reopened_seen:
        return
    reopened_seen.add(sit_row.situation_id)
    prev = store.latest_revision(sit_row.situation_id)
    if prev is None:
        return
    store.add_revision(
        dc_replace(
            prev,
            rev=0,  # add_revision が採番
            delta_type="reopened",
            delta_note="新着証拠の割当により dormant から再活性化",
            created_at=now_iso,
            run_id="",
        )
    )


def _judgment_anchor_keys(
    j: KeyJudgment,
    entities_by_id: dict[str, set[str]],
    title_by_id: dict[str, str],
) -> frozenset[str]:
    """判定の anchor キー (強 entity + 国) を証拠記事 entity と gazetteer で導出。"""
    keys: set[str] = set()
    nations: set[str] = set(nations_in_text(j.claim))
    for e in j.evidence:
        strong, ns = split_anchor_keys(frozenset(entities_by_id.get(e.article_id, set())))
        keys |= strong
        nations |= ns
        nations |= nations_in_text(title_by_id.get(e.article_id, ""))
    keys |= {f"involved_country:{n}" for n in nations}
    return frozenset(keys)


def _pir_ids_for_articles(
    article_ids: list[str], entities_by_id: dict[str, set[str]]
) -> tuple[str, ...]:
    """記事群の pir entity から PIR リンクを頻度順に導出 (決定論)。

    評価済み証拠に限らず割当記事全体を観測として使える (射影の再水和用)。
    """
    counts: dict[str, int] = {}
    for aid in article_ids:
        for k in entities_by_id.get(aid, set()):
            t, _, v = k.partition(":")
            if t == "pir" and v:
                counts[v] = counts.get(v, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return tuple(v for v, _ in ranked[:_MAX_PIR_IDS])


def _judgment_pir_ids(j: KeyJudgment, entities_by_id: dict[str, set[str]]) -> tuple[str, ...]:
    """証拠記事の pir entity から PIR リンクを頻度順に導出 (決定論)。"""
    return _pir_ids_for_articles([e.article_id for e in j.evidence], entities_by_id)


def _revision_from_judgment(
    j: KeyJudgment, *, situation_id: str, delta_type: DeltaType, delta_note: str, now_iso: str
) -> RevisionRow:
    # 整合不変量の最終ガード (2026-07-25): leading スカラとマトリクス verdict の二重表現は
    # ここ (台帳書込の唯一の seam) で必ず一致させる。上流の是正漏れは warn で可視化する
    # (静かに直すと上流バグが再発しても気付けない)。
    hyps = reconcile_hypotheses(j.hypotheses, j.leading_hypothesis)
    if hyps != j.hypotheses:
        _log.warning(
            "revision_coherence_corrected",
            situation=situation_id,
            leading=j.leading_hypothesis,
        )
    return RevisionRow(
        situation_id=situation_id,
        rev=0,  # add_revision が採番
        claim=j.claim,
        # LLM 分類 (ACH の claim_type)。欠落時は ongoing_activity = 減衰対象側 (保守的)
        claim_type=j.claim_type or "ongoing_activity",
        leading_hypothesis=j.leading_hypothesis,
        confidence=j.confidence,
        confidence_basis=j.confidence_basis,
        hypotheses_json=json.dumps(
            [
                {
                    "hypothesis": h.hypothesis,
                    "consistent": h.consistent,
                    "inconsistent": h.inconsistent,
                    "verdict": h.verdict,
                }
                for h in hyps
            ],
            ensure_ascii=False,
        ),
        assumptions_json=json.dumps(list(j.key_assumptions), ensure_ascii=False),
        missing_json=json.dumps(list(j.missing_evidence), ensure_ascii=False),
        indicators_json=json.dumps(list(j.indicators), ensure_ascii=False),
        implication=j.implication,
        delta_type=delta_type,
        delta_note=delta_note,
        created_at=now_iso,
    )


def _apply_judgments(
    *,
    est: Estimate,
    store: SituationStore,
    sit_keys: list[SituationKeys],
    entities_by_id: dict[str, set[str]],
    title_by_id: dict[str, str],
    now_iso: str,
) -> tuple[int, int, int, list[SituationKeys]]:
    """判定を台帳に反映。返り値: (opened, revisions, evidence_added, 更新後 sit_keys)。"""
    opened = revisions = evidence_added = 0
    for j in est.judgments:
        anchor_keys = _judgment_anchor_keys(j, entities_by_id, title_by_id)
        strong, nations = split_anchor_keys(anchor_keys)
        # claim 文の token も照合に使う (地政学判定は entity が乏しい)
        art_like = ArticleKeys(
            article_id="",
            title=j.claim,
            strong=strong,
            nations=nations,
            tokens=topic_tokens(j.claim),
        )
        # claim 照合は token のみ規則も使う (国/強 entity の無い claim の重複開設を防ぐ)
        matched = match_claim(art_like, sit_keys)
        if matched is None:
            row = store.open_situation(
                title=j.claim,
                domain=j.domain,
                anchors=anchor_keys,
                pir_ids=_judgment_pir_ids(j, entities_by_id),
                now_iso=now_iso,
            )
            delta: DeltaType
            decision: Literal["opened", "reopened"]
            if row.status in ("closed", "dormant"):
                # ゾンビ経路の根治 (監査 backlog 2026-07-05): 同一正規化 title が
                # closed/dormant 行に一致すると、従来は status がそのままで revision と
                # 証拠だけが吸い込まれ、射影 (active/dormant のみ) から永久に不可視だった。
                # 新規開設でなく reopen として可視化し、系譜 (revision 履歴) を保存する。
                store.reopen_situation(row.situation_id, now_iso=now_iso)
                row = dc_replace(row, status="active", closed_at=None, last_evidence_at=now_iso)
                delta = "reopened"
                note = "収束/休眠中の同一情勢が再出現 (reopen)"
                decision = "reopened"
            else:
                delta = "opened"
                note = "新規開設 (本期間の判定より)"
                decision = "opened"
                opened += 1
            store.log_detection(
                run_at=now_iso,
                article_id=j.evidence[0].article_id if j.evidence else "",
                decision=decision,
                reason=f"judgment {j.id}: {j.claim[:80]}",
                situation_id=row.situation_id,
            )
            sit_keys.append(build_situation_keys(row))
            target = row
        else:
            sit, _by = matched
            target = sit.row
            prev = store.latest_revision(target.situation_id)
            was_dormant = target.status == "dormant"
            delta = compute_delta_type(
                prev_leading=prev.leading_hypothesis if prev else None,
                prev_confidence=prev.confidence if prev else None,
                leading=j.leading_hypothesis,
                confidence=j.confidence,
                was_dormant=was_dormant,
            )
            note = (
                f"見立て {prev.leading_hypothesis}→{j.leading_hypothesis}"
                if prev and delta == "hypothesis_flip"
                else f"確度 {prev.confidence}→{j.confidence}"
                if prev and delta in ("strengthened", "weakened")
                else ""
            )
            store.touch_situation(target.situation_id, last_evidence_at=now_iso, status="active")
        store.add_revision(
            _revision_from_judgment(
                j,
                situation_id=target.situation_id,
                delta_type=delta,
                delta_note=note,
                now_iso=now_iso,
            )
        )
        revisions += 1
        for e in j.evidence:
            if store.record_assessment(
                situation_id=target.situation_id,
                article_id=e.article_id,
                assessed_at=now_iso,
                polarity=e.polarity,
                attribution_basis=e.attribution_basis,
                excerpt=e.excerpt,
                source_tier=e.source_tier,
            ):
                evidence_added += 1
    return opened, revisions, evidence_added, sit_keys


def _assign_pool(
    *,
    pool: list[dict[str, object]],
    store: SituationStore,
    sit_keys: list[SituationKeys],
    entities_by_id: dict[str, set[str]],
    now_iso: str,
) -> tuple[int, int, int, int]:
    """プール記事を既存 Situation に割当。返り値: (assigned, evidence, unassigned_high, un_med)。"""
    already = store.assigned_article_ids()
    assigned = evidence = un_high = un_med = 0
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
            # 監査台帳は high のみ行を残す (recall 監査対象、medium はカウントのみ = ログ肥大防止)
            importance = str(a.get("importance", "") or "")
            if importance == "high":
                un_high += 1
                store.log_detection(
                    run_at=now_iso,
                    article_id=aid,
                    decision="unassigned",
                    reason="既存 Situation に不適合 (段B の新規開設判断の対象)",
                )
            else:
                un_med += 1
            continue
        sit, by = matched
        if store.record_assignment(
            situation_id=sit.row.situation_id, article_id=aid, added_at=now_iso, assigned_by=by
        ):
            evidence += 1
        _reactivate_if_dormant(
            store=store, sit_row=sit.row, now_iso=now_iso, reopened_seen=reopened_seen
        )
        store.touch_situation(sit.row.situation_id, last_evidence_at=now_iso, status="active")
        assigned += 1
    return assigned, evidence, un_high, un_med


def _sweep_lifecycle(*, store: SituationStore, now: datetime) -> tuple[int, int]:
    """active→dormant / dormant→closed の自動遷移 (§2.3)。返り値: (dormant数, closed数)。"""
    dormant = closed = 0
    for row in store.load_situations(("active", "dormant")):
        # 常設情報要求 (standing) は dormant/close しない — 静穏期間こそ問いが生きる時間
        # (「静か≠安全」、設計 doc §2.2)。静穏の正直さは status でなく描画で表現する。
        if row.kind == "standing":
            continue
        try:
            last = datetime.fromisoformat(row.last_evidence_at)
        except ValueError:
            continue
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        idle_days = (now - last).total_seconds() / 86400
        dormant_after = _DORMANT_AFTER_DAYS.get(row.domain, _DORMANT_DEFAULT_DAYS)
        if row.status == "active" and idle_days >= dormant_after:
            store.set_status(row.situation_id, "dormant")
            dormant += 1
        elif row.status == "dormant" and idle_days >= dormant_after + _CLOSE_AFTER_DORMANT_DAYS:
            store.set_status(row.situation_id, "closed", closed_at=now.isoformat())
            # 収束判定自体を revision として残す (§2.3、朝刊「収束」の供給源)
            _add_closing_revision(
                store=store,
                situation_id=row.situation_id,
                idle_days=int(idle_days),
                now_iso=now.isoformat(),
            )
            closed += 1
    return dormant, closed


def update_ledger(
    *,
    est: Estimate,
    repo: RunHistoryRepository,
    store: SituationStore,
    db_path: Path,
    now: datetime | None = None,
) -> LedgerStats:
    """Estimate とプールを状況台帳に反映する (LLM 不使用・決定論)。

    呼出側 (grounded pipeline) は失敗を捕捉して synthesis を止めないこと (shadow 原則)。
    """
    from src.synthesis.generator import _build_high_importance_cross_axis, _resolve_period
    from src.synthesis.grounded.pipeline import _POOL_BY_PERIOD, _POOL_IMPORTANCE

    now_dt = now or datetime.now(UTC)
    now_iso = now_dt.isoformat()
    _s, _e, _label, lookback, _bw = _resolve_period(period_type=est.period_type, now=now_dt)
    pool = _build_high_importance_cross_axis(
        lookback_hours=lookback,
        db_path=db_path,
        limit=_POOL_BY_PERIOD.get(est.period_type, 150),
        importance_levels=_POOL_IMPORTANCE,
    )
    pool_ids = [str(a.get("article_id", "")) for a in pool if a.get("article_id")]
    # 判定側の証拠記事 (過去文脈などプール外を含む) も entity を引けるよう和集合で取得
    judgment_ids = [e.article_id for j in est.judgments for e in j.evidence]
    entities_by_id = repo.entity_keys_for_articles(list({*pool_ids, *judgment_ids}))
    title_by_id = {str(a.get("article_id", "")): str(a.get("title", "")) for a in pool}

    situations = store.load_situations(("active", "dormant"))
    sit_keys = [build_situation_keys(r) for r in situations]

    opened, revisions, ev_j, sit_keys = _apply_judgments(
        est=est,
        store=store,
        sit_keys=sit_keys,
        entities_by_id=entities_by_id,
        title_by_id=title_by_id,
        now_iso=now_iso,
    )
    assigned, ev_p, un_high, un_med = _assign_pool(
        pool=pool,
        store=store,
        sit_keys=sit_keys,
        entities_by_id=entities_by_id,
        now_iso=now_iso,
    )
    dormant, closed = _sweep_lifecycle(store=store, now=now_dt)
    store.purge_detection_log(
        before_iso=(now_dt - timedelta(days=_DETECTION_LOG_RETENTION_DAYS)).isoformat()
    )
    stats = LedgerStats(
        situations_before=len(situations),
        opened=opened,
        revisions=revisions,
        evidence_added=ev_j + ev_p,
        assigned_articles=assigned,
        unassigned_high=un_high,
        unassigned_medium=un_med,
        dormant_marked=dormant,
        closed_marked=closed,
    )
    _log.info(
        "situation_ledger_updated",
        period_type=est.period_type,
        situations_before=stats.situations_before,
        opened=stats.opened,
        revisions=stats.revisions,
        evidence_added=stats.evidence_added,
        assigned=stats.assigned_articles,
        unassigned_high=stats.unassigned_high,
        unassigned_medium=stats.unassigned_medium,
        dormant=stats.dormant_marked,
        closed=stats.closed_marked,
    )
    return stats
