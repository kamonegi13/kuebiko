"""夜間 deep-review — 当日更新された Situation の ACH を拡張思考 (think) で再評価する。

昼の増分 ACH (think OFF・即時) が立てた判定を、夜間のアイドル帯に think ON で
やり直し、精緻化された revision で台帳を上書きする。状態中心アーキテクチャの帰結:
台帳が canonical なので、翌朝の射影 (headline / 報告書 / ACH 表示) は自動的に
精緻判定を継承する。judge の「毎時増分 + 夜間一掃」と同型の時間シフト。

think A/B 2026-07-24 の根拠: 仮説マトリクスの全採点 (OFF は最適仮説を採点漏れ)・
認識論的仮定 (言及なし≠不在)・ソース批判で ON が優位。ただし 6-10 倍/呼出
(~1.5k→9.5k output tok) のためリアルタイム経路には載せない。

設計上の要点:
- **prior は「当日更新前」の revision** (`latest_revision_before`) — 当日証拠を
  ACH 集計へ二重加算しない (増分 ACH は加算方式のため、当日判定を prior にすると
  同じ証拠を 2 回数えてしまう)。
- **外部モデル割当時のみ実行** — ローカル gemma の thinking は空応答前歴があり、
  think なしの再評価は昼と同品質で無意味。reasoning ティアがローカルなら skip。
- adversarial ゲートは昼と同一 (think OFF の対称 red-team) を精緻判定にも通す。
- 証拠状態は昼と同じ規約で刻む (監査 2026-08-01): 接地 prompt に本文を供給した
  記事は mark_read、ACH 引用は record_assessment。旧「触らない」方針は mark_read の
  セマンティクス (供給の事実を刻む) と矛盾し、最大能力 (cap 20) のパスが未評価
  backlog を一切消化せず翌朝同じ記事を再読する純損失になっていた。fired_indicators
  も昼と同様に forecast 採点 (update_forecasts) へ同期する (夜間 hit の取りこぼし防止)。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.assessment.ledger import _revision_from_judgment
from src.assessment.situation_store import DeltaType, RevisionRow, SituationStore
from src.assessment.standing import (
    POSTURE_ACTIVE_JP,
    STANDING_KIND,
    has_jp_direct_evidence,
)
from src.assessment.stateful import (
    _build_source,
    _delta_note,
    _final_delta,
    _prior_view,
)
from src.cti.source_basis import classify_source_tier, compute_source_basis
from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository
from src.synthesis.grounded.adversary import adversarial_review, apply_adversarial
from src.synthesis.grounded.estimate import KeyJudgment, final_confidence
from src.synthesis.grounded.hypotheses import POSTURE_HYPOTHESES
from src.synthesis.grounded.incremental import incremental_ground_and_score
from src.synthesis.grounded.passes import ground_and_score

if TYPE_CHECKING:
    from src.tools.llm_client import LLMClient

_log = get_logger(__name__)

# 1 晩に精査する Situation 数の上限。~170s/件 × 20 ≈ 60 分 (max_runtime 75 分内)。
DEFAULT_CAP = 20
# 当日窓 (この時間内に revision が作られた Situation が対象)
LOOKBACK_HOURS = 24
# 1 Situation あたり ACH に渡す当日証拠の上限 (昼の増分と同じ思想の有界化)
_MAX_SOURCES = 8


def _select_targets(
    store: SituationStore,
    revs_by_sid: dict[str, list[RevisionRow]],
    *,
    since_iso: str,
    cap: int,
) -> tuple[list[tuple[str, list[str]]], int]:
    """当日 revision + 当日証拠のある active Situation を standing 優先で選ぶ。

    返り値 = ([(sid, 当日証拠 aids)], 証拠なしで除外した数)。証拠の有無を cap 消費前に
    フィルタする — sweep 等の判断のみの revision (再接地の材料なし) に枠を使わない
    (実機スモーク 2026-07-24: standing が判断のみ revision で cap を占有し実質 0 件になった)。
    """
    scored: list[tuple[int, int, str, list[str]]] = []
    skipped = 0
    for sid, revs in revs_by_sid.items():
        row = store.get_situation(sid)
        if row is None or row.status != "active":
            continue
        today_aids = store.evidence_ids_added_since(sid, since_iso=since_iso)
        if not today_aids:
            skipped += 1
            continue
        is_standing = 1 if row.kind == STANDING_KIND else 0
        scored.append((is_standing, len(revs), sid, today_aids))
    scored.sort(key=lambda t: (-t[0], -t[1], t[2]))
    return [(sid, aids) for _, _, sid, aids in scored[:cap]], skipped


async def run_deep_review(
    *,
    llm: LLMClient,
    adversarial_llm: LLMClient | None = None,
    repo: RunHistoryRepository | None = None,
    store: SituationStore | None = None,
    cap: int = DEFAULT_CAP,
    now: datetime | None = None,
    db_path: Path | str = Path("data/run_history.db"),
) -> dict[str, int]:
    """当日更新された Situation を think 品質で再評価し revision を upsert する。

    ``llm`` は think 注入済みの分析 client (呼出側が ThinkOnClient で包む)。
    ``adversarial_llm`` は red-team ゲート用 (None なら llm — ただし通常は
    think なしの素の分析 client を渡し、昼のゲートと同一条件に揃える)。
    返り値 = 統計 dict (reviewed / flips / conf_changes / skipped_no_sources / errors)。
    """
    repo_inst = repo or RunHistoryRepository()
    store_inst = store or SituationStore(db_path=db_path)
    now_dt = now or datetime.now(UTC)
    now_iso = now_dt.isoformat()
    since_iso = (now_dt - timedelta(hours=LOOKBACK_HOURS)).isoformat()

    revs_by_sid = store_inst.revisions_since(since_iso, until_iso=now_iso)
    targets, skipped_no_sources = _select_targets(
        store_inst, revs_by_sid, since_iso=since_iso, cap=cap
    )
    stats = {
        "candidates": len(revs_by_sid),
        "selected": len(targets),
        "reviewed": 0,
        "flips": 0,
        "conf_changes": 0,
        "skipped_no_sources": skipped_no_sources,
        "errors": 0,
    }

    pending: list[dict[str, Any]] = []
    for sid, today_aids in targets:
        row = store_inst.get_situation(sid)
        if row is None:
            continue
        sources: list[dict[str, str]] = []
        tier_by_id: dict[str, str] = {}
        read_aids: list[str] = []  # 接地 prompt に本文を供給した記事 (mark_read 対象)
        for aid in today_aids[:_MAX_SOURCES]:
            src = _build_source(repo_inst, aid)
            if src:
                sources.append(src)
                tier_by_id[aid] = classify_source_tier(src["feed_title"], src["feed_url"])
                read_aids.append(aid)
        if not sources:
            # 記事本文が取得不能 (削除等) で source 化できなかった稀ケース
            stats["skipped_no_sources"] += 1
            continue
        is_standing = row.kind == STANDING_KIND
        hyp_override = POSTURE_HYPOTHESES if is_standing else None
        # 二重加算防止の核心: prior は当日窓より前の revision。当日 opened なら None
        # (フル接地で当日判定をやり直す)。
        prior = store_inst.latest_revision_before(sid, until_iso=since_iso)
        latest_rev = store_inst.latest_revision(sid)  # 昼の判定 — delta/flip 比較の基準
        try:
            fired: tuple[str, ...] = ()
            if prior is None:
                analysis = await ground_and_score(
                    llm=llm,
                    claim=row.title,
                    domain=row.domain,
                    sources=sources,
                    tier_by_id=tier_by_id,
                    hypotheses_override=hyp_override,
                )
                claim = row.title
            else:
                inc = await incremental_ground_and_score(
                    llm=llm,
                    situation_title=row.title,
                    prior=_prior_view(prior, store_inst.evidence_excerpts(sid)),
                    domain=row.domain,
                    sources=sources,
                    tier_by_id=tier_by_id,
                    hypotheses_override=hyp_override,
                )
                analysis = inc.analysis
                claim = inc.claim
                fired = tuple(getattr(inc, "fired_indicators", ()) or ())
        except Exception as e:  # noqa: BLE001 — 1 Situation の失敗で夜間バッチを止めない
            _log.warning("deep_review_ach_failed", situation=sid, error=str(e))
            stats["errors"] += 1
            continue
        pending.append(
            {
                "sid": sid,
                "claim": claim,
                "domain": row.domain,
                "analysis": analysis,
                "standing": is_standing,
                "latest": latest_rev,
                "read_aids": read_aids,
                "fired": fired,
            }
        )

    if not pending:
        _log.info("deep_review_done", **stats)
        return stats

    # 昼の永続化と同じ後段: source_basis 合成 → posture cap → adversarial → revision
    judgments: list[KeyJudgment] = []
    meta_by_id: dict[str, dict[str, Any]] = {}
    for p in pending:
        sid = str(p["sid"])
        analysis = p["analysis"]
        basis_ids = list(store_inst.evidence_ids_by_situation([sid]).get(sid, set()))
        sb = compute_source_basis(repo_inst, basis_ids)
        conf, reason = final_confidence(
            analysis.llm_confidence, sb.confidence, analysis.leading_hypothesis, analysis.evidence
        )
        basis = f"ACH={analysis.llm_confidence} / source_basis={sb.confidence} / 夜間精査"
        if reason:
            basis = f"{basis} / {reason}"
        if (
            p["standing"]
            and analysis.leading_hypothesis == POSTURE_ACTIVE_JP
            and conf == "high"
            and not has_jp_direct_evidence(db_path=Path(db_path), situation_id=sid)
        ):
            conf = "moderate"
            basis = f"{basis} / posture_cap: JP直接証拠なしは moderate 上限"
        judgments.append(
            KeyJudgment(
                id=sid,
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
        meta_by_id[sid] = p

    try:
        reviews = await adversarial_review(llm=adversarial_llm or llm, judgments=tuple(judgments))
        judgments = [apply_adversarial(j, reviews.get(j.id)) for j in judgments]
    except Exception as e:  # noqa: BLE001 — red-team 失敗で精緻判定を捨てない (昼と同じ方針)
        _log.warning("deep_review_adversarial_failed", error=str(e))

    for j in judgments:
        p = meta_by_id[j.id]
        latest: RevisionRow | None = p["latest"]
        delta: DeltaType
        if latest is None:
            delta, note = "opened", "夜間精査 (初回判定)"
        else:
            delta = _final_delta(
                prev=latest,
                was_dormant=False,
                leading=j.leading_hypothesis,
                confidence=j.confidence,
                claim=j.claim,
                scope_expanded=False,
            )
            note = "夜間精査: " + _delta_note(latest, j, delta)
            if j.leading_hypothesis != latest.leading_hypothesis:
                stats["flips"] += 1
            if j.confidence != latest.confidence:
                stats["conf_changes"] += 1
        if p["fired"]:
            note = (note + " / " if note else "") + "指標発火: " + "; ".join(p["fired"])
        store_inst.add_revision(
            _revision_from_judgment(
                j,
                situation_id=j.id,
                delta_type=delta,
                delta_note=note,
                now_iso=now_iso,
            )
        )
        stats["reviewed"] += 1
        # 証拠状態を昼 (stateful) と同じ規約で刻む (監査 2026-08-01):
        # ACH 引用 = 評価 (record_assessment upsert)、接地 prompt 供給 = 読了 (mark_read)。
        # これが無いと cap 20 の夜間パスが backlog を消化せず、翌朝同じ記事を再読する。
        for ev in j.evidence:
            store_inst.record_assessment(
                situation_id=j.id,
                article_id=ev.article_id,
                assessed_at=now_iso,
                polarity=ev.polarity,
                attribution_basis=ev.attribution_basis,
                excerpt=ev.excerpt,
                source_tier=ev.source_tier,
            )
        read_aids_j = [str(a) for a in p.get("read_aids", [])]
        if read_aids_j:
            store_inst.mark_read(situation_id=j.id, article_ids=read_aids_j, read_at=now_iso)

    # forecast lifecycle 同期 (昼の stateful と同型): 夜間に観測した指標発火を hit として
    # 刻む。決定論・冪等・失敗しても deep-review 本体を止めない (補助系)。
    try:
        import json as _json

        from src.assessment.forecast import update_forecasts

        rows_now = store_inst.load_situations(("active", "dormant", "closed"))
        active_now = {r.situation_id for r in rows_now if r.status == "active"}
        closed_now = {r.situation_id for r in rows_now if r.status == "closed"}
        update_forecasts(
            store=store_inst,
            now=now_dt,
            latest_indicators_by_sid={
                sid: [str(i) for i in _json.loads(rev.indicators_json or "[]")]
                for sid in sorted(active_now)
                if (rev := store_inst.latest_revision(sid)) is not None
            },
            fired_by_sid={str(p["sid"]): list(p["fired"]) for p in pending if p["fired"]},
            active_sids=active_now,
            closed_sids=closed_now,
        )
    except Exception as e:  # noqa: BLE001
        _log.warning("deep_review_forecast_sync_failed", error=str(e))

    _log.info("deep_review_done", **stats)
    return stats
