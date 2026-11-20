"""台帳の未読の根拠の見張り (2026-11-20、11-19 監査 D2 P1-9)。

``situation_evidence.assessed_at`` は ACH が**引用した**根拠にだけ付く (読んだが引用しなかった
根拠も NULL のまま) ので、滞留の指標にならない。読まれていない根拠 = ``read_at IS NULL``
(weak を除く・active の状況だけ) を数え、日次ハートビートの 1 行に出す。

閾値は事前登録 (結果を見る前に決めた値): 未読 ``UNREAD_WARN`` 件超 または 最古が
``OLDEST_WARN_HOURS`` 時間超で ⚠。直近 7 日の実測は 追加 → 読まれるまで 中央値 0h・p90 2.6h。
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)

UNREAD_WARN = 50
OLDEST_WARN_HOURS = 12

_SQL = (
    "SELECT e.added_at FROM situation_evidence e"
    " JOIN situations s ON s.situation_id = e.situation_id"
    " WHERE e.read_at IS NULL AND e.weak_at IS NULL AND s.status = 'active'"
)


def _as_utc(value: object) -> datetime:
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def build_ledger_unread_line(
    repo: RunHistoryRepository, *, now: datetime | None = None
) -> str | None:
    """ハートビートの 1 行。失敗は None (ハートビート本体は止めない)。"""
    try:
        at = now or datetime.now(UTC)
        with repo._connect() as conn:  # noqa: SLF001 — 読み取りのみ
            rows = conn.execute(_SQL).fetchall()
        if not rows:
            return "台帳の未読の根拠: 0 件"
        oldest = min(_as_utc(r["added_at"]) for r in rows)
        hours = int((at - oldest).total_seconds() // 3600)
        warn = len(rows) > UNREAD_WARN or hours > OLDEST_WARN_HOURS
        mark = "⚠️" if warn else ""
        return f"{mark}台帳の未読の根拠: {len(rows)} 件 (最古 {hours}h)"
    except Exception as e:  # noqa: BLE001 — heartbeat 本体を止めない
        _log.warning("ledger_unread_line_failed", error_type=type(e).__name__)
        return None


__all__ = ["OLDEST_WARN_HOURS", "UNREAD_WARN", "build_ledger_unread_line"]
