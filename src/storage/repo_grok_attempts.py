"""Grok の描画されなかったレポートの取り直しの記録 (``grok_fetch_attempts``、2026-10-29 P0-25)。

メール (IMAP UID) ごとの連続の失敗回数・通知済みの印を持つ。メールの件名・本文は持たない。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from src.storage.repo_base import RunHistoryRepositoryBase
from src.storage.row_mappers import _to_iso

DEFAULT_RETENTION_DAYS = 30
MAX_REASON_CHARS = 100
_MARK_COLUMNS = frozenset({"capped_notified_at", "expiry_warned_at"})


@dataclass(frozen=True)
class GrokAttempt:
    email_uid: str
    received_at: str
    fail_count: int
    last_reason: str
    capped_notified_at: str | None
    expiry_warned_at: str | None


_COLUMNS = "email_uid, received_at, fail_count, last_reason, capped_notified_at, expiry_warned_at"


class GrokAttemptsMixin(RunHistoryRepositoryBase):
    """``grok_fetch_attempts`` テーブルの読み書き。"""

    def get_grok_attempt(self: Any, email_uid: str) -> GrokAttempt | None:
        with self._connect() as conn:
            r = conn.execute(
                f"SELECT {_COLUMNS} FROM grok_fetch_attempts WHERE email_uid = ?",  # noqa: S608
                (email_uid,),
            ).fetchone()
        if r is None:
            return None
        return GrokAttempt(
            email_uid=str(r["email_uid"]),
            received_at=str(r["received_at"]),
            fail_count=int(r["fail_count"]),
            last_reason=str(r["last_reason"]),
            capped_notified_at=r["capped_notified_at"],
            expiry_warned_at=r["expiry_warned_at"],
        )

    def record_grok_failure(
        self: Any,
        email_uid: str,
        received_at: datetime,
        reason: str,
        *,
        when: datetime | None = None,
    ) -> GrokAttempt:
        """失敗を 1 回数えて更新後の記録を返す (初回は行を作る)。"""
        at = _to_iso(when or datetime.now(UTC))
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO grok_fetch_attempts"
                " (email_uid, received_at, fail_count, first_failed_at, last_failed_at,"
                " last_reason) VALUES (?, ?, 1, ?, ?, ?)"
                " ON CONFLICT(email_uid) DO UPDATE SET"
                " fail_count = grok_fetch_attempts.fail_count + 1,"
                " last_failed_at = excluded.last_failed_at,"
                " last_reason = excluded.last_reason",
                (email_uid, _to_iso(received_at), at, at, reason[:MAX_REASON_CHARS]),
            )
        recorded: GrokAttempt | None = self.get_grok_attempt(email_uid)
        if recorded is None:  # 直前に書いた行が無い = DB の異常
            raise RuntimeError("grok_fetch_attempts の書き込みが読めない")
        return recorded

    def mark_grok_capped_notified(self: Any, email_uid: str, *, when: datetime) -> None:
        self._set_grok_mark("capped_notified_at", email_uid, when)

    def mark_grok_expiry_warned(self: Any, email_uid: str, *, when: datetime) -> None:
        self._set_grok_mark("expiry_warned_at", email_uid, when)

    def _set_grok_mark(self: Any, column: str, email_uid: str, when: datetime) -> None:
        if column not in _MARK_COLUMNS:
            raise ValueError(column)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE grok_fetch_attempts SET {column} = ? WHERE email_uid = ?",  # noqa: S608
                (_to_iso(when), email_uid),
            )

    def clear_grok_attempt(self: Any, email_uid: str) -> None:
        """成功したら記録を消す (数え直し)。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM grok_fetch_attempts WHERE email_uid = ?", (email_uid,))

    def purge_grok_fetch_attempts(self: Any, days: int = DEFAULT_RETENTION_DAYS) -> int:
        cutoff = _to_iso(datetime.now(UTC) - timedelta(days=days))
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM grok_fetch_attempts WHERE last_failed_at < ?", (cutoff,)
            )
            return int(cur.rowcount or 0)


__all__ = ["DEFAULT_RETENTION_DAYS", "GrokAttempt", "GrokAttemptsMixin"]
