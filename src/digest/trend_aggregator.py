"""Phase G1: trend 構造集計。

過去 7 日の articles を actor / topic pattern で集計し、累積急増・跨 source 一致・
継続案件節目を機械的に検出する。LLM の役割は命名と物語化に絞り、検出ロジック自体は
決定的・再現可能にする。

設計:
    - actor pattern は hardcode (memory: cti_pir.md の PIR 重視軸を反映)
    - dry-run (2026-05-18) で Ukraine/Hormuz/Salt Typhoon 等の trend が data 駆動で
      検出可能なことを実証済。
    - 跨 source の signal は ≥ 3 source 言及、spike は base rate ≥ 2x で判定。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.logging_config import get_logger

_log = get_logger(__name__)

DEFAULT_DB_PATH = Path("data/run_history.db")

# Phase G1: trend 抽出対象 pattern (memory: cti_pir + 5T-V dry-run 結果)
# 名前 → 正規表現 (title マッチに使う、case-insensitive)
TREND_PATTERNS: dict[str, str] = {
    # APT (PIR 1)
    "China APT (Salt Typhoon / Volt Typhoon / APT41)": (
        r"Salt[\s-]?Typhoon|Volt[\s-]?Typhoon|APT[\s-]?41|UAT-\d+|SHADOW-EARTH"
    ),
    "North Korea APT (Lazarus / Kimsuky / ScarCruft)": (
        r"Lazarus|Kimsuky|ScarCruft|APT[\s-]?37|RGB|North[\s]?Korea"
    ),
    "Iran APT (MuddyWater / APT34 等)": r"MuddyWater|APT[\s-]?34|Iranian APT|Seedworm",
    "Russia APT (APT28 / APT29 / Sandworm)": (
        r"APT[\s-]?28|APT[\s-]?29|Fancy Bear|Cozy Bear|Sandworm|Turla"
    ),
    # 地政・国際情勢 (5T-V dry-run で多 source 検出済)
    "Iran 関連 / ホルムズ海峡": r"\bIran(?!ian APT)|ホルムズ|Hormuz|タンカー|Tehran",
    "Ukraine 関連": r"Ukraine|ウクライナ|Kyiv|Kiev|Zelensky",
    "Taiwan / 中台情勢": r"Taiwan|台湾|Taipei|Chinese coercion",
    # サイバー犯罪 / ハクティビスト
    "ShinyHunters / 大規模 leak": r"ShinyHunters|Shiny Hunters",
    "LockBit / ALPHV / BlackCat": r"LockBit|BlackCat|ALPHV|Royal ransomware",
    "Genesis / Akira ransomware": r"Genesis Ransomware|\bAkira\b",
    # 技術トレンド
    "supply chain attack": r"supply[\s-]?chain|サプライチェーン",
    "zero-day exploit": r"zero[\s-]?day|ゼロデイ|0[\s-]?day",
    "AI / LLM security": r"\bLLM\b|\bAI agent|エージェント.*セキュリティ|prompt injection",
    # 日本特化 (PIR 1 強)
    "日本標的事案": r"日本(政府|防衛|自衛隊|企業)|日系大手|JSDF|日本標的",
}


@dataclass(frozen=True)
class TrendArticle:
    """trend cluster に属する 1 article の最小情報。"""

    article_id: str
    title: str
    url: str
    feed_title: str
    posted_channel: str
    created_at: str
    discord_message_id: str | None = None
    discord_channel_id: str | None = None


@dataclass(frozen=True)
class TrendCluster:
    """1 つの trend cluster (1 命題に対応)。"""

    name: str
    pattern: str
    total_count: int  # lookback window 内の件数
    baseline_avg: float  # 過去 baseline_weeks 平均件数
    spike_ratio: float  # total_count / baseline_avg (新規 actor は inf)
    is_new: bool  # baseline=0 (= 新規出現)
    is_cross_source: bool  # ≥3 source で言及
    sources: tuple[str, ...]
    articles: tuple[TrendArticle, ...] = field(default_factory=tuple)


def _connect(db_path: Path) -> Any:
    """Phase Y-3: DATABASE_URL set なら PG、 未設定なら SQLite。"""
    from src.storage.db_backend import connect as backend_connect

    con = backend_connect(db_path)
    if hasattr(con, "row_factory"):
        con.row_factory = sqlite3.Row
    return con


def _fetch_articles(
    *,
    since: datetime,
    until: datetime,
    db_path: Path,
) -> list[sqlite3.Row]:
    """期間内の posted article (channel 問わず) を取得。"""
    sql = """
        SELECT article_id, title, url, feed_title, feed_url, posted_channel,
               discord_message_id, discord_channel_id, created_at
          FROM articles
         WHERE created_at >= ? AND created_at < ?
           AND status = 'posted'
         ORDER BY created_at DESC
    """
    with _connect(db_path) as con:
        return list(con.execute(sql, (since.isoformat(), until.isoformat())).fetchall())


def _count_by_pattern(
    rows: list[sqlite3.Row],
    pattern_str: str,
) -> tuple[list[sqlite3.Row], set[str], set[str]]:
    """pattern に title が match する rows と、表示用 feed 集合 + 安定キー集合を返す。

    表示用 (title_sources) は feed_title、cross-source 判定用 (key_sources) は安定キー
    feed_url (未充足なら feed_title) で distinct。改名で同一 source が 2 重カウントされ
    cross-source 閾値を誤発火するのを防ぐ (Source Identity Decoupling #4)。
    """
    pat = re.compile(pattern_str, re.IGNORECASE)
    matched: list[sqlite3.Row] = []
    title_sources: set[str] = set()
    key_sources: set[str] = set()
    for r in rows:
        if pat.search(r["title"] or ""):
            matched.append(r)
            title = r["feed_title"] or ""
            title_sources.add(title)
            key_sources.add(r["feed_url"] or title)
    return matched, title_sources, key_sources


def aggregate_trends(
    *,
    lookback_hours: int = 168,
    baseline_weeks: int = 4,
    min_count: int = 3,
    min_sources_for_cross: int = 3,
    spike_threshold: float = 2.0,
    now: datetime | None = None,
    db_path: Path = DEFAULT_DB_PATH,
    patterns: dict[str, str] | None = None,
    max_articles_per_cluster: int = 5,
) -> list[TrendCluster]:
    """過去 ``lookback_hours`` の articles を pattern で集計し trend cluster を返す。

    cluster 採用基準:
        - total_count >= min_count
        - かつ (spike_ratio >= spike_threshold OR is_new OR is_cross_source)

    Args:
        lookback_hours: 現在 window (default 168h = 7d)
        baseline_weeks: 比較対象の過去窓数 (default 4 週)
        min_count: cluster 採用する最小件数
        min_sources_for_cross: 跨 source 判定の閾値
        spike_threshold: spike 判定の比率閾値
        now: 基準時刻 (テスト用)
        db_path: SQLite path
        patterns: 上書き用 (default: TREND_PATTERNS)
        max_articles_per_cluster: 各 cluster の代表 article 上限

    Returns:
        spike_ratio 降順の TrendCluster list (採用基準を満たすもののみ)。
    """
    base = now or datetime.now(UTC)
    pats = patterns or TREND_PATTERNS
    current_since = base - timedelta(hours=lookback_hours)
    baseline_since = base - timedelta(hours=lookback_hours * (baseline_weeks + 1))

    current_rows = _fetch_articles(since=current_since, until=base, db_path=db_path)
    baseline_rows = _fetch_articles(
        since=baseline_since,
        until=current_since,
        db_path=db_path,
    )

    clusters: list[TrendCluster] = []
    for name, pattern_str in pats.items():
        cur_matched, cur_sources, cur_keys = _count_by_pattern(current_rows, pattern_str)
        base_matched, _, _ = _count_by_pattern(baseline_rows, pattern_str)

        total_count = len(cur_matched)
        baseline_avg = len(base_matched) / max(1, baseline_weeks)
        is_new = baseline_avg == 0 and total_count > 0
        spike_ratio = (
            float("inf") if is_new else (total_count / baseline_avg if baseline_avg else 0)
        )
        # cross-source 判定は安定キー (feed_url) の distinct 数で (改名の 2 重カウント防止)
        is_cross = len(cur_keys) >= min_sources_for_cross

        # 採用判定
        if total_count < min_count:
            continue
        if not (is_new or spike_ratio >= spike_threshold or is_cross):
            continue

        articles = [
            TrendArticle(
                article_id=r["article_id"],
                title=r["title"] or "",
                url=r["url"] or "",
                feed_title=r["feed_title"] or "",
                posted_channel=r["posted_channel"] or "",
                created_at=r["created_at"] or "",
                # sqlite3.Row は .get() を持たないため if-in で防御。SIM401 は誤検知。
                discord_message_id=(
                    r["discord_message_id"]  # noqa: SIM401
                    if "discord_message_id" in r
                    else None
                ),
                discord_channel_id=(
                    r["discord_channel_id"]  # noqa: SIM401
                    if "discord_channel_id" in r
                    else None
                ),
            )
            for r in cur_matched[:max_articles_per_cluster]
        ]
        clusters.append(
            TrendCluster(
                name=name,
                pattern=pattern_str,
                total_count=total_count,
                baseline_avg=baseline_avg,
                spike_ratio=spike_ratio,
                is_new=is_new,
                is_cross_source=is_cross,
                sources=tuple(sorted(cur_sources)),
                articles=tuple(articles),
            ),
        )

    # is_new > spike_ratio > total_count の優先順
    clusters.sort(
        key=lambda c: (
            0 if c.is_new else 1,
            -c.spike_ratio if c.spike_ratio != float("inf") else 0,
            -c.total_count,
        ),
    )
    _log.info(
        "trend_aggregation_complete",
        lookback_hours=lookback_hours,
        baseline_weeks=baseline_weeks,
        clusters_found=len(clusters),
        patterns_evaluated=len(pats),
    )
    return clusters
