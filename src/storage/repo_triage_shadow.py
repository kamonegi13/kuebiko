"""triage 影子記録 (triage_shadow テーブル、2026-10-08、M4)。

s23 投入前に「本番 (現行) triage の判定」と「平たい triage + 取り込み時ヒント」を
並べて記録する安全網。本番の取り込み判定・配信は変えない (記録のみ)。
docs/importance_relevance_redesign.md §6 の go/no-go 判定の材料。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from src.storage.repo_base import RunHistoryRepositoryBase
from src.storage.row_mappers import _to_iso

DEFAULT_LIST_LIMIT = 100
_TITLE_MAX = 300
_REASONS_MAX = 500


@dataclass(frozen=True)
class TriageShadowRow:
    """影子記録 1 件。``new_kept`` = ``flat_importance`` が medium 以上 OR ``hint_fired``。"""

    article_id: str
    url: str
    title: str
    feed_title: str
    feed_url: str
    current_importance: str
    current_kept: bool
    flat_importance: str
    hint_fired: bool
    hint_reasons: tuple[str, ...]
    new_kept: bool
    ts: str = ""


@dataclass(frozen=True)
class TriageShadowSummary:
    """§6 の 2x2 (current_kept × new_kept) 集計。"""

    days: int
    both_kept: int
    current_only: int
    new_only: int
    both_dropped: int

    @property
    def total(self) -> int:
        return self.both_kept + self.current_only + self.new_only + self.both_dropped


def _cutoff(days: int) -> str:
    return _to_iso(datetime.now(UTC) - timedelta(days=days))


class TriageShadowMixin(RunHistoryRepositoryBase):
    """triage_shadow テーブルの読み書き。"""

    def record_triage_shadow(self, rows: Sequence[TriageShadowRow]) -> int:
        """影子記録をまとめて追記する (append-only)。空入力は 0。"""
        if not rows:
            return 0
        ts = _to_iso(datetime.now(UTC))
        values = [
            (
                r.article_id,
                r.url,
                r.title[:_TITLE_MAX],
                r.feed_title,
                r.feed_url,
                r.current_importance,
                1 if r.current_kept else 0,
                r.flat_importance,
                1 if r.hint_fired else 0,
                ",".join(r.hint_reasons)[:_REASONS_MAX],
                1 if r.new_kept else 0,
                ts,
            )
            for r in rows
        ]
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO triage_shadow"
                " (article_id, url, title, feed_title, feed_url, current_importance,"
                " current_kept, flat_importance, hint_fired, hint_reasons, new_kept,"
                " created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
        return len(values)

    def summarize_triage_shadow(self, *, days: int) -> TriageShadowSummary:
        """直近 ``days`` 日の 2x2 (current_kept × new_kept) を集計する。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT current_kept, new_kept, COUNT(*) AS n FROM triage_shadow"
                " WHERE created_at >= ? GROUP BY current_kept, new_kept",
                (_cutoff(days),),
            ).fetchall()
        both_kept = current_only = new_only = both_dropped = 0
        for r in rows:
            cur_kept, new_kept, n = bool(r[0]), bool(r[1]), int(r[2])
            if cur_kept and new_kept:
                both_kept += n
            elif cur_kept and not new_kept:
                current_only += n
            elif not cur_kept and new_kept:
                new_only += n
            else:
                both_dropped += n
        return TriageShadowSummary(
            days=days,
            both_kept=both_kept,
            current_only=current_only,
            new_only=new_only,
            both_dropped=both_dropped,
        )

    def list_triage_shadow_disagreements(
        self, *, days: int, limit: int = DEFAULT_LIST_LIMIT
    ) -> list[TriageShadowRow]:
        """現行判定と新ルールの判定が食い違った記事を新しい順に返す (画面/API 用)。"""
        sql = (
            "SELECT article_id, url, title, feed_title, feed_url, current_importance,"
            " current_kept, flat_importance, hint_fired, hint_reasons, new_kept, created_at"
            " FROM triage_shadow WHERE created_at >= ? AND current_kept <> new_kept"
            " ORDER BY created_at DESC, id DESC LIMIT ?"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, (_cutoff(days), int(limit))).fetchall()
        return [
            TriageShadowRow(
                article_id=str(r["article_id"]),
                url=str(r["url"]),
                title=str(r["title"]),
                feed_title=str(r["feed_title"]),
                feed_url=str(r["feed_url"]),
                current_importance=str(r["current_importance"]),
                current_kept=bool(r["current_kept"]),
                flat_importance=str(r["flat_importance"]),
                hint_fired=bool(r["hint_fired"]),
                hint_reasons=tuple(x for x in str(r["hint_reasons"]).split(",") if x),
                new_kept=bool(r["new_kept"]),
                ts=str(r["created_at"]),
            )
            for r in rows
        ]

    def purge_triage_shadow(self, days: int = 60) -> int:
        """``days`` 日より古い影子記録を削除する。"""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM triage_shadow WHERE created_at < ?", (_cutoff(days),))
            return int(cur.rowcount or 0)


__all__ = [
    "DEFAULT_LIST_LIMIT",
    "TriageShadowRow",
    "TriageShadowSummary",
    "TriageShadowMixin",
]
