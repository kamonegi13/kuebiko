"""DB 衛生の日次バッチ (監査 2026-07-05)。

retention purge が「コンテナ起動時のみ」発火していた問題への対処: deploy が止まると
retention も止まる。深夜の日次 cron で purge 一式を回す (READ_ONLY instance は scheduler
自体を起動しないので二重実行しない)。VACUUM は既存の 30 日 sentinel 制御を流用する。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from src.logging_config import get_logger

if TYPE_CHECKING:
    from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)


def body_purge_days() -> int:
    """旗 BODY_PURGE_DAYS を読む。未設定・不正値 (負・非数) は 0 (purge しない) に倒す。"""
    raw = os.environ.get(BODY_PURGE_DAYS_ENV, "0").strip() or "0"
    try:
        days = int(raw)
    except ValueError:
        _log.warning("body_purge_days_invalid", value_type="non_integer")
        return 0
    if days < 0:
        _log.warning("body_purge_days_invalid", value_type="negative")
        return 0
    return days


# situation_detection_log の retention は ledger.py と同一 SSoT (replay の材料、365 日)
# detect_bundles (束ねの記録、M2) は評価の正解づくりに使うので長めに残す
_DETECT_BUNDLES_RETENTION_DAYS = 180
# articles.body / body_ja / linked_context の retention。2026-10-29 に既定を「消さない」
# (0) へ変更 (利用者判断 J1: 過去の記事を検索できるよう本文はローカル Tier2 に全て残す)。
# 公開面 (標準・アドバンスド) が本文を持たない関門は不変 (CLAUDE.md §9)。旗
# BODY_PURGE_DAYS: 0 = purge しない / N>0 = N 日より古い本文を GC-root つきで NULL 化。
BODY_PURGE_DAYS_ENV = "BODY_PURGE_DAYS"
# 認証の監査証跡 (2026-08-02)。run_logs より長い 180 日 — 不正アクセスの発覚は
# 遅れる前提で、遡って調べられる期間を確保する。
_ACCESS_AUDIT_RETENTION_DAYS = 180
# ops 通知の永続化 (2026-08-21)。access_audit と同じ 180 日水準 — 運用上の警告も
# 「後から遡って調べる」ためのものなので同じ保持期間にする。
_OPS_NOTICES_RETENTION_DAYS = 180
# 意味 dedup の skip 記録 (2026-08-23、docs/event_news_design.md §8b/§11)。replay の
# 材料として 365 日残す (2026-10-29、dedup 本体の 90 日とは切り離す)。
_SEMANTIC_SKIPS_RETENTION_DAYS = 365
# triage の落選記録 (2026-10-02)。誤った落選は発覚が遅れる (媒体を後から見直したとき)
# ので、監査証跡と同水準の 180 日保つ。
_TRIAGE_REJECTIONS_RETENTION_DAYS = 180
# triage 影子記録 (2026-10-08、M4)。go/no-go 判断用の短命な観察データ (§6) なので
# replay の材料として 180 日残す (2026-10-29。purge_triage_shadow の既定と合わせる)。
_TRIAGE_SHADOW_RETENTION_DAYS = 180
# routing v2 の影子記録 (2026-10-10、M3b 段1)。triage_shadow と同じ go/no-go 用の
# 短命な観察データなので同水準の 60 日。
_ROUTING_V2_SHADOW_RETENTION_DAYS = 180
_ALERT_DEDUP_SHADOW_RETENTION_DAYS = 180
_SEVERITY_C_SHADOW_RETENTION_DAYS = 180


async def run_daily_maintenance(repo: RunHistoryRepository | None = None) -> None:
    """日次 DB 衛生: retention purge 一式。失敗しても例外は上げない (scheduler を守る)。"""
    from src.ui.app import DEDUP_RETENTION_DAYS  # 既存の retention 定数を単一 SSoT で流用

    try:
        from src.storage.run_history import RunHistoryRepository

        repo = repo or RunHistoryRepository()
        purged_logs = repo.purge_old_logs()
        purged_dedup = repo.purge_old_dedup_entries(days=DEDUP_RETENTION_DAYS)
        detection_purged = _purge_detection_log(repo)
        purged_bodies = _purge_bodies(repo)
        # 監査証跡は run_logs (30 日) より長く保つ — 事故の発覚は遅れる前提 (2026-08-02)
        purged_audit = repo.purge_old_access_audit(days=_ACCESS_AUDIT_RETENTION_DAYS)
        # ops 通知も同じ理由で access_audit と同水準の retention (2026-08-21)
        purged_ops_notices = repo.purge_old_ops_notices(days=_OPS_NOTICES_RETENTION_DAYS)
        # 意味 dedup の skip 記録 (2026-08-23、§8b) — dedup 本体と同水準の 90 日で連動
        purged_semantic_skips = repo.purge_semantic_skips(days=_SEMANTIC_SKIPS_RETENTION_DAYS)
        purged_triage_rejections = repo.purge_triage_rejections(
            days=_TRIAGE_REJECTIONS_RETENTION_DAYS
        )
        purged_triage_shadow = repo.purge_triage_shadow(days=_TRIAGE_SHADOW_RETENTION_DAYS)
        purged_routing_v2_shadow = repo.purge_routing_v2_shadow(
            days=_ROUTING_V2_SHADOW_RETENTION_DAYS
        )
        purged_alert_dedup_shadow = repo.purge_alert_dedup_shadow(
            days=_ALERT_DEDUP_SHADOW_RETENTION_DAYS
        )
        purged_severity_c_shadow = repo.purge_severity_c_shadow(
            days=_SEVERITY_C_SHADOW_RETENTION_DAYS
        )
        # 評価資産 (ラベル/評価記録/goldset) を日次で data/backups へ退避。
        # 失敗は module 内で握る (fail-open) — 衛生バッチを止めない
        from src.storage.asset_export import export_eval_assets

        export_eval_assets(repo)
        # 主体の決定論補完の安全網 (主経路は ransomware_ingest 直後)。
        # 週次収穫ジョブ撤収 (2026-08-22) に伴い日次衛生へ移設した
        backfill = _run_subject_backfill_safety_net(repo)
        _log.info(
            "daily_maintenance_done",
            purged_logs=purged_logs,
            purged_dedup=purged_dedup,
            purged_detection_log=detection_purged,
            purged_bodies=purged_bodies,
            purged_access_audit=purged_audit,
            purged_ops_notices=purged_ops_notices,
            purged_semantic_skips=purged_semantic_skips,
            purged_triage_rejections=purged_triage_rejections,
            purged_triage_shadow=purged_triage_shadow,
            purged_routing_v2_shadow=purged_routing_v2_shadow,
            purged_alert_dedup_shadow=purged_alert_dedup_shadow,
            purged_severity_c_shadow=purged_severity_c_shadow,
            subject_backfilled=backfill,
        )
    except Exception as e:  # noqa: BLE001 — 衛生バッチの失敗で scheduler を汚さない
        _log.error("daily_maintenance_failed", error=str(e))


def _run_subject_backfill_safety_net(repo: RunHistoryRepository) -> int:
    """犯行声明突合による主体補完の日次安全網。失敗は 0 (fail-open)。"""
    try:
        from src.cti.subject_backfill import run_subject_backfill

        return run_subject_backfill(repo).filled
    except Exception as e:  # noqa: BLE001 — 補完の失敗で衛生バッチを止めない
        _log.warning("subject_backfill_safety_net_failed", error=str(e))
        return 0


def _purge_bodies(repo: RunHistoryRepository) -> int:
    """本文の purge。旗 0 (既定) では呼ばず、スキップを 1 行ログに残す。"""
    days = body_purge_days()
    if days == 0:
        _log.info("body_purge_disabled", flag=BODY_PURGE_DAYS_ENV)
        return 0
    return repo.purge_article_bodies_older_than(days=days)


def _purge_detection_log(repo: RunHistoryRepository) -> int:
    """situation_detection_log の 365 日 retention (台帳稼働時のみ意味を持つ)。

    detect_bundles の purge は別の try (こちらの失敗で本流の purge を止めない)。
    """
    from src.assessment.ledger import _DETECTION_LOG_RETENTION_DAYS
    from src.assessment.situation_store import SituationStore

    try:
        cutoff = (datetime.now(UTC) - timedelta(days=_DETECTION_LOG_RETENTION_DAYS)).isoformat()
        store = SituationStore(db_path=repo.db_path)
        purged = store.purge_detection_log(before_iso=cutoff)
    except Exception as e:  # noqa: BLE001 — 台帳不在/未初期化でも他の purge は継続
        _log.warning("detection_log_purge_skipped", error=type(e).__name__)
        return 0
    try:
        bundles_cutoff = (
            datetime.now(UTC) - timedelta(days=_DETECT_BUNDLES_RETENTION_DAYS)
        ).isoformat()
        bundles = store.purge_detect_bundles(before_iso=bundles_cutoff)
        _log.info("detect_bundles_purged", purged=bundles)
    except Exception as e:  # noqa: BLE001 — 記録表の purge 失敗は衛生バッチを止めない
        _log.warning("detect_bundles_purge_failed", error=type(e).__name__)
    return purged
