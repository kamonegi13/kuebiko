"""Phase H: PMESII-PT 可視化 (V1 Axis Dashboard / V3 Timeline) 用の集計サービス。

articles テーブルの新 column (pmesii_* / victim_*) を主軸に、
8 軸ごとの件数 / spike / top entity / 直近 incident を SQL で集計する。
LLM 不要、軽量で UI から都度呼び出し可能。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.logging_config import get_logger

_log = get_logger(__name__)

DEFAULT_DB_PATH = Path("data/run_history.db")

# PMESII 7 軸の (canonical id, display, column name)。
# T (Time) 軸は 2026-07-16 に廃止 — 付与率 ~30% で弁別力がなく、時間次元は
# event_date / compromise_date が一級フィールドとして担う。DB 列 pmesii_t は
# 履歴保全のため残る (表示・新規書込のみ撤去)。
PMESII_AXIS_DISPLAY: list[tuple[str, str, str]] = [
    ("P", "P: 政治", "pmesii_p"),
    ("M", "M: 軍事", "pmesii_m"),
    ("E", "E: 経済", "pmesii_e"),
    ("S", "S: 社会", "pmesii_s"),
    ("I-infra", "I: インフラ", "pmesii_i_infra"),
    ("I-cyber", "I: 情報・サイバー", "pmesii_i_cyber"),
    ("P-env", "P: 物理環境", "pmesii_p_env"),
]


@dataclass(frozen=True)
class RecentIncident:
    """軸 card に表示する直近 incident 1 件。"""

    article_id: str
    title: str
    url: str
    feed_title: str
    importance: str | None
    posted_channel: str | None
    created_at: str
    discord_message_id: str | None = None
    discord_channel_id: str | None = None


@dataclass(frozen=True)
class TopEntity:
    """entity ranking 1 件 (sector / country など)。"""

    label: str  # canonical id or display name
    count: int


@dataclass(frozen=True)
class AxisCard:
    """1 軸分の dashboard card data。"""

    axis_id: str
    display: str
    column: str
    total_current: int  # lookback window 内の件数
    total_baseline_avg: float  # 過去 baseline_weeks 平均
    spike_ratio: float  # current / baseline (新規時は inf)
    is_new: bool
    is_spike: bool  # spike_ratio >= threshold
    top_sectors: list[TopEntity]
    top_countries: list[TopEntity]
    recent_incidents: list[RecentIncident]


def _connect(db_path: Path) -> Any:
    """Phase Y-3: DATABASE_URL set なら PG、 未設定なら SQLite。"""
    from src.storage.db_backend import connect as backend_connect

    con = backend_connect(db_path)
    if hasattr(con, "row_factory"):
        con.row_factory = sqlite3.Row
    return con


def _count_axis_in_window(
    con: sqlite3.Connection,
    axis_column: str,
    since: datetime,
    until: datetime,
) -> int:
    sql = (
        f"SELECT COUNT(*) AS n FROM articles "  # noqa: S608 (column 名は内部固定 list)
        f"WHERE {axis_column}=1 AND status='posted' "
        f"AND datetime(created_at) >= datetime(?) "
        f"AND datetime(created_at) < datetime(?)"
    )
    row = con.execute(sql, (since.isoformat(), until.isoformat())).fetchone()
    return int(row["n"]) if row and row["n"] is not None else 0


def _top_entities_in_window(
    con: sqlite3.Connection,
    axis_column: str,
    field: str,
    since: datetime,
    until: datetime,
    limit: int = 5,
) -> list[TopEntity]:
    """軸内で field (victim_sector_canonical or victim_country_iso) 別の上位 entity。"""
    sql = (
        f"SELECT {field} AS label, COUNT(*) AS n FROM articles "  # noqa: S608
        f"WHERE {axis_column}=1 AND status='posted' "
        f"AND {field} IS NOT NULL "
        f"AND datetime(created_at) >= datetime(?) "
        f"AND datetime(created_at) < datetime(?) "
        f"GROUP BY {field} "
        f"ORDER BY n DESC LIMIT ?"
    )
    rows = con.execute(
        sql,
        (since.isoformat(), until.isoformat(), limit),
    ).fetchall()
    return [TopEntity(label=str(r["label"]), count=int(r["n"])) for r in rows]


def _recent_incidents_for_axis(
    con: sqlite3.Connection,
    axis_column: str,
    since: datetime,
    until: datetime,
    limit: int = 5,
) -> list[RecentIncident]:
    sql = (
        f"SELECT article_id, title, url, feed_title, importance, "
        f"posted_channel, created_at, discord_message_id, discord_channel_id "
        f"FROM articles "  # noqa: S608
        f"WHERE {axis_column}=1 AND status='posted' "
        f"AND datetime(created_at) >= datetime(?) "
        f"AND datetime(created_at) < datetime(?) "
        f"ORDER BY datetime(created_at) DESC LIMIT ?"
    )
    rows = con.execute(
        sql,
        (since.isoformat(), until.isoformat(), limit),
    ).fetchall()
    keys_set: set[str] = set(rows[0].keys()) if rows else set()
    out: list[RecentIncident] = []
    for r in rows:
        dmid = r["discord_message_id"] if "discord_message_id" in keys_set else None
        dcid = r["discord_channel_id"] if "discord_channel_id" in keys_set else None
        out.append(
            RecentIncident(
                article_id=str(r["article_id"]),
                title=str(r["title"] or ""),
                url=str(r["url"] or ""),
                feed_title=str(r["feed_title"] or ""),
                importance=r["importance"],
                posted_channel=r["posted_channel"],
                created_at=str(r["created_at"]),
                discord_message_id=dmid,
                discord_channel_id=dcid,
            ),
        )
    return out


def fetch_axis_dashboard(
    *,
    lookback_hours: int = 168,
    baseline_weeks: int = 4,
    spike_threshold: float = 2.0,
    now: datetime | None = None,
    db_path: Path = DEFAULT_DB_PATH,
    top_entity_limit: int = 5,
    recent_incident_limit: int = 5,
) -> list[AxisCard]:
    """V1 Axis Dashboard 用に 8 軸分の AxisCard を返す。

    各軸:
        - 今週件数 (lookback_hours)
        - 前 baseline_weeks 平均
        - spike_ratio (current / avg)、新規 (avg=0) は inf
        - top sectors (victim_sector_canonical で集計)
        - top countries (victim_country_iso で集計)
        - 直近 incident 上位
    """
    base = now or datetime.now(UTC)
    current_since = base - timedelta(hours=lookback_hours)
    baseline_since = base - timedelta(hours=lookback_hours * (baseline_weeks + 1))

    cards: list[AxisCard] = []
    with _connect(db_path) as con:
        for axis_id, display, column in PMESII_AXIS_DISPLAY:
            total_current = _count_axis_in_window(con, column, current_since, base)
            total_baseline = _count_axis_in_window(
                con,
                column,
                baseline_since,
                current_since,
            )
            baseline_avg = total_baseline / max(1, baseline_weeks)
            is_new = baseline_avg == 0 and total_current > 0
            spike_ratio = (
                float("inf") if is_new else (total_current / baseline_avg if baseline_avg else 0.0)
            )
            is_spike = is_new or spike_ratio >= spike_threshold
            top_sectors = _top_entities_in_window(
                con,
                column,
                "victim_sector_canonical",
                current_since,
                base,
                top_entity_limit,
            )
            top_countries = _top_entities_in_window(
                con,
                column,
                "victim_country_iso",
                current_since,
                base,
                top_entity_limit,
            )
            incidents = _recent_incidents_for_axis(
                con,
                column,
                current_since,
                base,
                recent_incident_limit,
            )
            cards.append(
                AxisCard(
                    axis_id=axis_id,
                    display=display,
                    column=column,
                    total_current=total_current,
                    total_baseline_avg=baseline_avg,
                    spike_ratio=spike_ratio,
                    is_new=is_new,
                    is_spike=is_spike,
                    top_sectors=top_sectors,
                    top_countries=top_countries,
                    recent_incidents=incidents,
                ),
            )
    _log.info(
        "axis_dashboard_fetched",
        lookback_hours=lookback_hours,
        baseline_weeks=baseline_weeks,
        cards=len(cards),
        non_empty_cards=sum(1 for c in cards if c.total_current > 0),
    )
    return cards


def resolve_incident_link(
    incident: RecentIncident,
    *,
    guild_id: str | None = None,
) -> str:
    """Discord post URL 優先、なければ元 URL fallback。"""
    if guild_id and incident.discord_channel_id and incident.discord_message_id:
        return (
            f"https://discord.com/channels/{guild_id}/"
            f"{incident.discord_channel_id}/{incident.discord_message_id}"
        )
    return incident.url
