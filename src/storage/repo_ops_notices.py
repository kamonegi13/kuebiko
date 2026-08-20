"""ops 通知の永続化 (ops_notices テーブル、run_history 分割の一部)。

``src.ui.services.ops_notify.post_ops_message`` が Discord ops webhook へ送る通知を
DB にも残す。webhook 不達・未設定・送信例外でも必ず 1 行残ることで、access_audit
(2026-08-02) と同型の教訓「stdout は監査にならない」「成功も記録しないと沈黙の意味を
決められない」をこの通知経路にも適用する。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from src.storage.repo_base import RunHistoryRepositoryBase
from src.storage.row_mappers import _to_iso


class OpsNoticesMixin(RunHistoryRepositoryBase):
    """ops_notices テーブルの読み書き。"""

    def record_ops_notice(
        self,
        *,
        title: str,
        body: str,
        importance: str = "low",
        sent: bool,
        when: datetime | None = None,
    ) -> None:
        """ops 通知を 1 行追記する (append-only)。

        呼び出し側 (ops_notify._persist_ops_notice) は失敗を握って warning に落とす —
        監査の書き込み失敗で通知そのものの送信を止めない (access_audit と同じ方針)。
        """
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO ops_notices (created_at, title, body, importance, sent)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    _to_iso(when or datetime.now(UTC)),
                    title[:200],
                    body[:4000],
                    importance,
                    1 if sent else 0,
                ),
            )

    def list_ops_notices(self, limit: int = 50) -> list[dict[str, Any]]:
        """ops 通知を新しい順に返す (運用画面「履歴・監査」タブ用)。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, created_at, title, body, importance, sent"
                " FROM ops_notices ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "created_at": str(r["created_at"]),
                "title": str(r["title"]),
                "body": str(r["body"]),
                "importance": str(r["importance"]),
                "sent": bool(r["sent"]),
            }
            for r in rows
        ]

    def purge_old_ops_notices(self, days: int = 180) -> int:
        """N 日より古い ops 通知を削除する (access_audit と同じ 180 日水準)。"""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM ops_notices WHERE created_at < datetime('now', ?)",
                (f"-{days} days",),
            )
            return int(cur.rowcount or 0)
