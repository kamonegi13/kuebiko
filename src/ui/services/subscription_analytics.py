"""Subscription 統計集計 (Phase 5T-Q)。

config/sources/feeds.yaml の購読 list と当ツールの articles table を join して、
各 feed の運用統計 (post 件数 / channel 振分け / importance 分布 /
dedup skip 率) を出して低貢献 feed を見える化する。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.logging_config import get_logger

_log = get_logger(__name__)

DEFAULT_DB_PATH = Path("data/run_history.db")
DEFAULT_LOOKBACK_DAYS = 30

# 低貢献判定閾値 (Phase 5T-Q)
_LOW_CONTRIB_MAX_POSTS = 3  # 30 日で 3 件以下なら 低貢献
_HIGH_DUP_RATE_THRESHOLD = 0.8  # dedup skip 率 80%+ なら "高重複"
_WATCH_ONLY_THRESHOLD = 1.0  # post 全件 watch ch (alert/brief 0) なら "watch only"

# Phase 5T-R: subscription が新しいときは no-articles 等を抑制する閾値
_NEW_SUBSCRIPTION_DAYS = 30


@dataclass(frozen=True)
class FeedStats:
    """1 feed あたりの運用統計 snapshot。"""

    feed_title: str  # articles.feed_title 値
    posted_count: int
    dup_skipped: int
    alert_count: int
    brief_count: int
    watch_count: int
    high_count: int
    medium_count: int
    low_count: int
    # 本文完全性 (2026-07-27): このソースの記事のうち全文取得できた数 / 切り株 (feed 抜粋) 数。
    # 「フィードは取れるが本文が全て切り株」(GBHackers 型 = source health は緑でも本文は痩せる)
    # をソース単位で可視化する。
    full_body_count: int = 0
    stump_count: int = 0
    # 層別健全性 (2026-08-01): feed 取得は成功しているのに本文抽出で全滅している
    # ソース (JS チャレンジ導入等) を「30日 記事なし」と区別するための観測点。
    # 例: Indo-Pacific Defense Forum — feed 200 / 記事レコードあり / extract_failed 15 件
    # が「要対処 (記事なし)」に潰れて、壊れた層 (本文取得) が読めなかった。
    extract_failed_count: int = 0
    # Phase 5T-R: 当ツール articles 表での初回観測時刻 (ISO 8601 string、未観測なら None)
    first_seen_at: str | None = None
    # Phase 5T-R: Inoreader 側 firstitemmsec から導出した datetime (微妙な意味合いで参考程度)
    inoreader_first_item_at: datetime | None = None

    @property
    def total_articles(self) -> int:
        """post + dup skip の合算 (pipeline 経由 article 総数)。"""
        return self.posted_count + self.dup_skipped

    @property
    def dup_rate(self) -> float:
        """dedup skip 率 (0.0-1.0)。total 0 なら 0.0。"""
        if self.total_articles == 0:
            return 0.0
        return self.dup_skipped / self.total_articles

    @property
    def watch_only_rate(self) -> float:
        """post のうち watch ch 行きの比率。post 0 なら 0.0。"""
        if self.posted_count == 0:
            return 0.0
        return self.watch_count / self.posted_count

    @property
    def body_evaluated_count(self) -> int:
        """全文 or 切り株のいずれかで body を持つ記事数 (本文取得率の母数)。"""
        return self.full_body_count + self.stump_count

    @property
    def full_body_rate(self) -> float:
        """本文を持つ記事のうち全文取得できた比率 (0.0-1.0)。母数 0 なら 1.0 (中立)。"""
        if self.body_evaluated_count == 0:
            return 1.0
        return self.full_body_count / self.body_evaluated_count

    @property
    def stump_rate(self) -> float:
        """本文を持つ記事のうち切り株 (feed 抜粋) の比率。母数 0 なら 0.0。"""
        if self.body_evaluated_count == 0:
            return 0.0
        return self.stump_count / self.body_evaluated_count

    @property
    def is_recently_subscribed(self) -> bool:
        """Phase 5T-R: 新規 subscription かを判定。

        判定の優先順:
            1. articles 表初回観測が _NEW_SUBSCRIPTION_DAYS 日内 → 新規
            2. inoreader_first_item_at が _NEW_SUBSCRIPTION_DAYS 日内 → 新規
            3. どちらも無い OR 古い → 既存
        """
        cutoff = datetime.now(UTC) - timedelta(days=_NEW_SUBSCRIPTION_DAYS)
        if self.first_seen_at:
            try:
                first_dt = datetime.fromisoformat(self.first_seen_at.replace("Z", "+00:00"))
                if first_dt.tzinfo is None:
                    first_dt = first_dt.replace(tzinfo=UTC)
                if first_dt >= cutoff:
                    return True
            except (ValueError, TypeError):
                pass
        return bool(self.inoreader_first_item_at and self.inoreader_first_item_at >= cutoff)

    @property
    def quality_score(self) -> int:
        """Phase C: feed の総合品質を 0-100 で評価。

        加点 (合計 100):
            - posted volume: max 30 (30 件で満点)
            - high importance: max 35 (5 件で満点)
            - medium importance: max 20 (15 件で満点)
            - alert ch ヒット: max 15 (3 件で満点)
        減点:
            - dup_rate: × 15 (80%+ dup なら -12)
            - watch_only_rate: × 10 (全 watch なら -10)

        新規 subscription (30 日内) は data 不足のため 50 を return (中立)。
        no-articles の旧 feed は 0 (要対処)。
        """
        if self.is_recently_subscribed:
            return 50
        if self.total_articles == 0:
            return 0
        volume_pts = min(self.posted_count / 30.0, 1.0) * 30
        high_pts = min(self.high_count / 5.0, 1.0) * 35
        med_pts = min(self.medium_count / 15.0, 1.0) * 20
        alert_pts = min(self.alert_count / 3.0, 1.0) * 15
        dup_penalty = self.dup_rate * 15
        watch_penalty = self.watch_only_rate * 10
        raw = volume_pts + high_pts + med_pts + alert_pts - dup_penalty - watch_penalty
        return max(0, min(100, int(round(raw))))

    @property
    def low_contrib_labels(self) -> list[str]:
        """低貢献マーカ (複数該当あり)。

        Phase 5T-R: subscription が新規なら no-articles / no-posts / low-volume を
        抑制し、代わりに "new" マーカを付与する (誤って unsubscribe 提案しないため)。
        """
        labels: list[str] = []
        is_new = self.is_recently_subscribed
        if is_new:
            labels.append("new")
        if self.total_articles == 0:
            if not is_new:
                labels.append("no-articles")
        elif self.posted_count == 0:
            if not is_new:
                labels.append("no-posts")
        elif self.posted_count <= _LOW_CONTRIB_MAX_POSTS and not is_new:
            labels.append("low-volume")
        if self.dup_rate >= _HIGH_DUP_RATE_THRESHOLD and self.total_articles >= 5:
            labels.append("high-dup")
        if self.watch_only_rate >= _WATCH_ONLY_THRESHOLD and self.posted_count >= 5:
            labels.append("watch-only")
        return labels


def fetch_all_feed_stats(
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, FeedStats]:
    """全 feed の統計を取得 (feed_title をキーとした dict)。

    Args:
        lookback_days: 集計期間 (デフォルト 30 日)
        db_path: SQLite ファイルパス

    Returns:
        {feed_title: FeedStats} — articles table に出現した feed_title のみ。
        Inoreader subscription に存在しても articles に出ていない feed は含まれない。
    """
    # Phase 5T-R: first_seen_at は全期間で取得 (lookback_days に依存しない)。
    # lookback 期間の集計と JOIN で「初回観測」を毎 feed に付与。
    # Source Identity Decoupling Stage 3: 結合キーを可変な feed_title でなく安定キー
    # feed_url にする。feed_url 未充足の旧記事は feed_title に fallback (COALESCE)。
    # SELECT の別名は feed_title のまま (下流 Python・dict キーを変えずに済む)。
    # 集計の単位が「source の安定 url」になり、表示名 (feed_title) 改名で統計が割れない。
    _skey = "COALESCE(NULLIF(feed_url, ''), feed_title, '')"
    sql = f"""
        WITH recent AS (
            SELECT
                {_skey} AS feed_title,
                SUM(CASE WHEN status = 'posted' THEN 1 ELSE 0 END) AS posted_count,
                SUM(CASE WHEN status = 'skipped_duplicate' THEN 1 ELSE 0 END) AS dup_skipped,
                SUM(CASE WHEN status = 'posted' AND posted_channel = 'alert' THEN 1 ELSE 0 END)
                    AS alert_count,
                SUM(CASE WHEN status = 'posted' AND posted_channel = 'brief' THEN 1 ELSE 0 END)
                    AS brief_count,
                SUM(CASE WHEN status = 'posted' AND posted_channel = 'watch' THEN 1 ELSE 0 END)
                    AS watch_count,
                SUM(CASE WHEN status = 'posted' AND importance = 'high' THEN 1 ELSE 0 END)
                    AS high_count,
                SUM(CASE WHEN status = 'posted' AND importance = 'medium' THEN 1 ELSE 0 END)
                    AS medium_count,
                SUM(CASE WHEN status = 'posted' AND importance = 'low' THEN 1 ELSE 0 END)
                    AS low_count,
                SUM(CASE WHEN body_source IN
                    ('full_extract','playwright_extract','prefetch','scraper')
                    THEN 1 ELSE 0 END) AS full_body_count,
                SUM(CASE WHEN body_source = 'feed_summary' THEN 1 ELSE 0 END) AS stump_count,
                SUM(CASE WHEN status = 'extract_failed' THEN 1 ELSE 0 END)
                    AS extract_failed_count
            FROM articles
            WHERE created_at >= datetime('now', ?)
            GROUP BY {_skey}
        ),
        first_seen AS (
            SELECT
                {_skey} AS feed_title,
                MIN(created_at) AS first_seen_at
            FROM articles
            GROUP BY {_skey}
        )
        SELECT
            r.feed_title AS feed_title,
            r.posted_count, r.dup_skipped,
            r.alert_count, r.brief_count, r.watch_count,
            r.high_count, r.medium_count, r.low_count,
            r.full_body_count, r.stump_count, r.extract_failed_count,
            f.first_seen_at AS first_seen_at
        FROM recent r
        LEFT JOIN first_seen f ON r.feed_title = f.feed_title
    """  # noqa: S608 (_skey は固定定数。lookback_days はパラメータバインド)
    # lookback はバインドパラメータ化 (security-review L2: SQL 組立規律の統一)。
    # translate_sql が datetime('now', ?) → PG の (NOW() + (?)::interval) に変換する。
    lookback_param = f"-{int(lookback_days)} day"
    out: dict[str, FeedStats] = {}
    from src.storage.db_backend import connect as _backend_connect

    with _backend_connect(db_path) as con:
        if hasattr(con, "row_factory"):
            con.row_factory = sqlite3.Row
        cur = con.cursor()
        try:
            cur.execute(sql, (lookback_param,))
            rows = cur.fetchall()
        except Exception as e:  # noqa: BLE001 — PG(psycopg)/SQLite 両 backend の例外を捕捉
            # sqlite3.Error のみだと PG の psycopg 例外を取りこぼし /subscriptions が 500 化する
            _log.warning("subscription_analytics_query_failed", error=str(e))
            return out
    for r in rows:
        title = r["feed_title"] or ""
        if not title:
            continue
        out[title] = FeedStats(
            feed_title=title,
            posted_count=int(r["posted_count"] or 0),
            dup_skipped=int(r["dup_skipped"] or 0),
            alert_count=int(r["alert_count"] or 0),
            brief_count=int(r["brief_count"] or 0),
            watch_count=int(r["watch_count"] or 0),
            high_count=int(r["high_count"] or 0),
            medium_count=int(r["medium_count"] or 0),
            low_count=int(r["low_count"] or 0),
            full_body_count=int(r["full_body_count"] or 0),
            stump_count=int(r["stump_count"] or 0),
            extract_failed_count=int(r["extract_failed_count"] or 0),
            first_seen_at=r["first_seen_at"] if "first_seen_at" in r.keys() else None,  # noqa: SIM118
        )
    return out


def feed_title_key_index(
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, str]:
    """feed_title (小文字化) → stats キー (feed_url 優先) の索引。

    購読 URL と記事に記録された feed_url が異なる source (watcher の autodiscovery 等) の
    stats を、title の大文字小文字無視一致で購読側へ別名結合するために使う (2026-07-12)。
    同名 title が複数キーに割れる場合は記事数の多い方を採用 (決定論)。
    """
    from src.storage.db_backend import connect as _backend_connect

    since = datetime.now(UTC) - timedelta(days=lookback_days)
    sql = """
        SELECT COALESCE(NULLIF(feed_url, ''), feed_title, '') AS skey,
               feed_title, COUNT(*) AS n
          FROM articles
         WHERE feed_title IS NOT NULL AND feed_title <> ''
           AND datetime(created_at) >= datetime(?)
         GROUP BY 1, 2
         ORDER BY n ASC
    """
    out: dict[str, str] = {}
    with _backend_connect(db_path) as con:
        if hasattr(con, "row_factory"):
            con.row_factory = sqlite3.Row
        for r in con.execute(sql, (since.isoformat(),)).fetchall():
            # n 昇順で上書き = 最終的に「記事数最大のキー」が残る
            out[str(r["feed_title"]).strip().lower()] = str(r["skey"])
    return out
