"""常設の問いの根拠を「直接 (日本・同盟国が被害) / 間接 (それ以外)」で数える (2026-10-22)。

印は記事の被害国から導出する (列は足さない — 被害国が後から直っても印が追従する)。
区分の SSoT は ``src/cti/alliance.py`` (= config/cti/countries.yaml)。
答えの重みは変えない。数えて見せるだけ (旗 ``STANDING_EVIDENCE_PROXIMITY``)。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from src.cti.alliance import (
    PROXIMITY_DIRECT,
    PROXIMITY_INDIRECT,
    PROXIMITY_UNKNOWN,
    evidence_proximity,
)


def summarize_proximity(victim_countries: Iterable[str | None]) -> dict[str, int]:
    """被害国の並び → {direct, indirect, unknown} の件数。"""
    counts = {PROXIMITY_DIRECT: 0, PROXIMITY_INDIRECT: 0, PROXIMITY_UNKNOWN: 0}
    for iso in victim_countries:
        counts[evidence_proximity(iso)] += 1
    return counts


def count_proximity(conn: Any, situation_id: str, *, since_iso: str) -> dict[str, int]:
    """問い 1 件の、窓内の根拠の直接 / 間接の件数。

    articles は同一 article_id が複数行ありうる (fan-out) ので article_id で 1 件に畳む。
    """
    rows = conn.execute(
        "SELECT e.article_id, MAX(a.victim_country_iso)"
        " FROM situation_evidence e JOIN articles a ON a.article_id = e.article_id"
        " WHERE e.situation_id = ? AND datetime(a.created_at) >= datetime(?)"
        " GROUP BY e.article_id",
        (situation_id, since_iso),
    ).fetchall()
    return summarize_proximity(r[1] for r in rows)


#: 問い別の集計 1 行 = (situation_id, title, {direct, indirect, unknown})
ProximityByQuestion = list[tuple[str, str, dict[str, int]]]

_WINDOW_SQL = (
    "SELECT e.situation_id, s.title, e.article_id, MAX(a.victim_country_iso)"
    " FROM situation_evidence e"
    " JOIN situations s ON s.situation_id = e.situation_id"
    " JOIN articles a ON a.article_id = e.article_id"
    " WHERE s.kind = 'standing' AND e.assessed_at IS NOT NULL"
    " AND datetime(e.assessed_at) >= datetime(?)"
    " GROUP BY e.situation_id, s.title, e.article_id"
)


def window_rows_sql() -> str:
    """評価に使われた根拠 (assessed_at が窓内) を問い × 記事で畳んで引く SELECT。"""
    return _WINDOW_SQL


def proximity_by_question(
    rows: Iterable[tuple[str, str, str, str | None]],
) -> ProximityByQuestion:
    """(situation_id, title, article_id, 被害国) の並び → 問い別の直接 / 間接の件数。"""
    grouped: dict[str, tuple[str, list[str | None]]] = {}
    for sid, title, _aid, country in rows:
        grouped.setdefault(sid, (title, []))[1].append(country)
    return [(sid, title, summarize_proximity(c)) for sid, (title, c) in sorted(grouped.items())]
