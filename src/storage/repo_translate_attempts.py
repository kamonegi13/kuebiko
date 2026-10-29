"""翻訳キューの失敗の印 (``translate_attempts`` テーブル、2026-10-29 P0-24)。

毎時のバックログ翻訳は新しい順に 40 件を選ぶ。写しの判定などで失敗し続ける記事が先頭に
居座ると、後ろの記事が処理されない (先頭詰まり)。失敗回数と最後の失敗時刻を持たせ、
回数に応じた間隔を空けるまで選ばないようにする。本文・訳文は持たない。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from src.storage.repo_base import RunHistoryRepositoryBase
from src.storage.row_mappers import _to_iso

DEFAULT_RETENTION_DAYS = 90
#: この回数までの失敗は間を空けずに再試行する
FREE_RETRY_FAILS = 3
#: 失敗回数 -> 次に選ぶまでの待ち時間 (時間)。24h・72h・7 日
BACKOFF_HOURS_BY_FAILS: dict[int, int] = {3: 24, 4: 72, 5: 168}
#: これを超えた失敗回数の記事は選ばない (body_ja は NULL のまま、記録だけ残る)
MAX_FAIL_COUNT = max(BACKOFF_HOURS_BY_FAILS)
MAX_REASON_CHARS = 200


def _backoff_clause_and_params(now: datetime) -> tuple[str, list[str]]:
    """バックオフ中の記事を除く SQL 句 (テーブル別名 ``t``) とパラメータ。"""
    parts = ["t.article_id IS NULL", f"t.fail_count < {int(FREE_RETRY_FAILS)}"]
    params: list[str] = []
    for fails, hours in sorted(BACKOFF_HOURS_BY_FAILS.items()):
        parts.append(f"(t.fail_count = {int(fails)} AND t.last_failed_at < ?)")
        params.append(_to_iso(now - timedelta(hours=hours)))
    return "(" + " OR ".join(parts) + ")", params


class TranslateAttemptsMixin(RunHistoryRepositoryBase):
    """``translate_attempts`` テーブルの読み書きと、バックオフ付きの未訳キュー。"""

    def record_translate_failure(
        self: Any, article_id: str, reason: str, *, when: datetime | None = None
    ) -> None:
        """失敗を 1 回数える (初回は行を作る)。理由は型名など本文を含まない短い文字列。"""
        at = _to_iso(when or datetime.now(UTC))
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO translate_attempts"
                " (article_id, fail_count, last_failed_at, last_reason) VALUES (?, 1, ?, ?)"
                " ON CONFLICT(article_id) DO UPDATE SET"
                " fail_count = translate_attempts.fail_count + 1,"
                " last_failed_at = excluded.last_failed_at,"
                " last_reason = excluded.last_reason",
                (article_id, at, reason[:MAX_REASON_CHARS]),
            )

    def clear_translate_attempts(self: Any, article_id: str) -> None:
        """成功したら印を消す (数え直し)。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM translate_attempts WHERE article_id = ?", (article_id,))

    def get_translate_attempt(self: Any, article_id: str) -> tuple[int, str, str] | None:
        """(fail_count, last_failed_at, last_reason)。印が無ければ None。"""
        with self._connect() as conn:
            r = conn.execute(
                "SELECT fail_count, last_failed_at, last_reason FROM translate_attempts"
                " WHERE article_id = ?",
                (article_id,),
            ).fetchone()
        if r is None:
            return None
        return int(r["fail_count"]), str(r["last_failed_at"]), str(r["last_reason"])

    def list_articles_untranslated_backoff(
        self: Any, limit: int = 40, *, now: datetime | None = None
    ) -> list[str]:
        """``list_articles_untranslated`` からバックオフ中・上限超えの記事を除いたもの。"""
        clause, params = _backoff_clause_and_params(now or datetime.now(UTC))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT a.article_id, MAX(a.created_at) AS latest FROM articles a"
                " LEFT JOIN translate_attempts t ON t.article_id = a.article_id"
                " WHERE a.body IS NOT NULL AND a.body != '' AND a.body_ja IS NULL"
                f" AND {clause}"  # noqa: S608 — 定数から組んだ句
                " GROUP BY a.article_id ORDER BY latest DESC LIMIT ?",
                (*params, int(limit)),
            ).fetchall()
        return [str(r["article_id"]) for r in rows]

    def purge_translate_attempts(self: Any, days: int = DEFAULT_RETENTION_DAYS) -> int:
        cutoff = _to_iso(datetime.now(UTC) - timedelta(days=days))
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM translate_attempts WHERE last_failed_at < ?", (cutoff,))
            return int(cur.rowcount or 0)


__all__ = [
    "BACKOFF_HOURS_BY_FAILS",
    "DEFAULT_RETENTION_DAYS",
    "FREE_RETRY_FAILS",
    "MAX_FAIL_COUNT",
    "TranslateAttemptsMixin",
]
