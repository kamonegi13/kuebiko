"""DB 衛生の日次バッチ (監査 2026-07-05)。

retention purge が「コンテナ起動時のみ」発火していた問題への対処: deploy が止まると
retention も止まる。深夜の日次 cron で purge 一式を回す (READ_ONLY instance は scheduler
自体を起動しないので二重実行しない)。VACUUM は既存の 30 日 sentinel 制御を流用する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from src.logging_config import get_logger

if TYPE_CHECKING:
    from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)

# situation_detection_log の retention (選定台帳、設計 §3.3)
_DETECTION_LOG_RETENTION_DAYS = 90
# articles.body の retention (schema 記載の 90 日、監査 backlog 2026-07-05 で実装)
_BODY_RETENTION_DAYS = 90
# 認証の監査証跡 (2026-08-02)。run_logs より長い 180 日 — 不正アクセスの発覚は
# 遅れる前提で、遡って調べられる期間を確保する。
_ACCESS_AUDIT_RETENTION_DAYS = 180
# ops 通知の永続化 (2026-08-21)。access_audit と同じ 180 日水準 — 運用上の警告も
# 「後から遡って調べる」ためのものなので同じ保持期間にする。
_OPS_NOTICES_RETENTION_DAYS = 180


async def run_daily_maintenance(repo: RunHistoryRepository | None = None) -> None:
    """日次 DB 衛生: retention purge 一式。失敗しても例外は上げない (scheduler を守る)。"""
    from src.ui.app import DEDUP_RETENTION_DAYS  # 既存の retention 定数を単一 SSoT で流用

    try:
        from src.storage.run_history import RunHistoryRepository

        repo = repo or RunHistoryRepository()
        purged_logs = repo.purge_old_logs()
        purged_dedup = repo.purge_old_dedup_entries(days=DEDUP_RETENTION_DAYS)
        detection_purged = _purge_detection_log(repo)
        purged_bodies = repo.purge_article_bodies_older_than(days=_BODY_RETENTION_DAYS)
        # 監査証跡は run_logs (30 日) より長く保つ — 事故の発覚は遅れる前提 (2026-08-02)
        purged_audit = repo.purge_old_access_audit(days=_ACCESS_AUDIT_RETENTION_DAYS)
        # ops 通知も同じ理由で access_audit と同水準の retention (2026-08-21)
        purged_ops_notices = repo.purge_old_ops_notices(days=_OPS_NOTICES_RETENTION_DAYS)
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


def _purge_detection_log(repo: RunHistoryRepository) -> int:
    """situation_detection_log の 90 日 retention (台帳稼働時のみ意味を持つ)。"""
    try:
        from src.assessment.situation_store import SituationStore

        cutoff = (datetime.now(UTC) - timedelta(days=_DETECTION_LOG_RETENTION_DAYS)).isoformat()
        return SituationStore(db_path=repo.db_path).purge_detection_log(before_iso=cutoff)
    except Exception as e:  # noqa: BLE001 — 台帳不在/未初期化でも他の purge は継続
        _log.warning("detection_log_purge_skipped", error=str(e))
        return 0
