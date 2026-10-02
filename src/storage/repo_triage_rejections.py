"""triage の落選記録 (triage_rejections テーブル、2026-10-02)。

落選した記事は articles に行を作らず URL 既読化だけされていたため、誤った落選を後から
確かめる手段が無かった (理由は 30 日で消える run_logs にしか無い)。理由つきで残し、
購読ソースの画面で媒体ごとに読む (件数 = 「取得はできているが全部落ちた」の区別、
一覧 = 落とし方が正しいかの確認)。retention 180 日 (daily-maintenance)。

時刻は ISO8601 UTC の文字列で持ち、締め切りも同じ書式の文字列で比べる
(SQLite / PG のどちらでも同じ結果になる)。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from src.storage.repo_base import RunHistoryRepositoryBase
from src.storage.row_mappers import _to_iso

#: 一覧の既定上限 (画面で読む量)
DEFAULT_LIST_LIMIT = 100
_TITLE_MAX = 300
_REASON_MAX = 500


@dataclass(frozen=True)
class TriageRejectionRow:
    """落選 1 件。``feed_url`` は媒体の安定キー (空なら feed_title で代替して数える)。"""

    article_id: str
    url: str
    title: str
    feed_title: str
    feed_url: str
    importance: str
    reason: str
    ts: str = ""

    @property
    def feed_key(self) -> str:
        """購読ソース統計と同じ結合キー (feed_url、無ければ feed_title)。"""
        return self.feed_url or self.feed_title


def _cutoff(days: int) -> str:
    return _to_iso(datetime.now(UTC) - timedelta(days=days))


class TriageRejectionsMixin(RunHistoryRepositoryBase):
    """triage_rejections テーブルの読み書き。"""

    def record_triage_rejections(
        self, rows: Sequence[TriageRejectionRow], *, when: datetime | None = None
    ) -> int:
        """落選をまとめて追記する (append-only)。空入力は 0。"""
        if not rows:
            return 0
        ts = _to_iso(when or datetime.now(UTC))
        values = [
            (
                r.article_id,
                r.url,
                r.title[:_TITLE_MAX],
                r.feed_title,
                r.feed_url,
                r.importance,
                r.reason[:_REASON_MAX],
                ts,
            )
            for r in rows
        ]
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO triage_rejections"
                " (article_id, url, title, feed_title, feed_url, importance, reason, ts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
        return len(values)

    def list_triage_rejections(
        self, *, days: int, feed_key: str | None = None, limit: int = DEFAULT_LIST_LIMIT
    ) -> list[TriageRejectionRow]:
        """直近 ``days`` 日の落選を新しい順に返す。``feed_key`` で媒体を絞る。"""
        sql = (
            "SELECT article_id, url, title, feed_title, feed_url, importance, reason, ts"
            " FROM triage_rejections WHERE ts >= ?"
        )
        params: list[object] = [_cutoff(days)]
        if feed_key:
            sql += " AND (feed_url = ? OR (feed_url = '' AND feed_title = ?))"
            params += [feed_key, feed_key]
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [
            TriageRejectionRow(
                article_id=str(r["article_id"]),
                url=str(r["url"]),
                title=str(r["title"]),
                feed_title=str(r["feed_title"]),
                feed_url=str(r["feed_url"]),
                importance=str(r["importance"]),
                reason=str(r["reason"]),
                ts=str(r["ts"]),
            )
            for r in rows
        ]

    def count_triage_rejections_by_feed(self, *, days: int) -> dict[str, int]:
        """直近 ``days`` 日の落選件数を媒体の結合キーごとに返す。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT CASE WHEN feed_url <> '' THEN feed_url ELSE feed_title END AS k,"
                " COUNT(*) AS n FROM triage_rejections WHERE ts >= ?"
                " GROUP BY CASE WHEN feed_url <> '' THEN feed_url ELSE feed_title END",
                (_cutoff(days),),
            ).fetchall()
        return {str(r["k"]): int(r["n"]) for r in rows if r["k"]}

    def purge_triage_rejections(self, days: int = 180) -> int:
        """``days`` 日より古い落選記録を削除する。"""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM triage_rejections WHERE ts < ?", (_cutoff(days),))
            return int(cur.rowcount or 0)


__all__ = ["DEFAULT_LIST_LIMIT", "TriageRejectionRow", "TriageRejectionsMixin"]
