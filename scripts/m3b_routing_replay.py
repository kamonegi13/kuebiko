"""M3b 段1: 過去の投稿済み記事に M3B_RULES を当て直し、現行ルールとの食い違いを見る (read-only)。

対象: 直近 N 日 (既定 30)、posted_channel が非 NULL、``article_importance_v2`` に記録済みの
記事 (同 article_id は最新 1 行)。``articles`` に保存済みの値 (importance / article_type /
posted_channel / routing_rule_id) と ``article_importance_v2`` (severity / severity_basis /
jp / relevant) から ``RoutingSignals`` を組み立て、``src/cti/routing_v2.M3B_RULES`` を評価する。

DB への書き込みは一切行わない (SELECT のみ)。

使い方::

    uv run python scripts/m3b_routing_replay.py [--days 30] [--samples 5]

または (本番 DB に read-only で当てる場合、コンテナ内)::

    docker compose run --rm --no-deps -T -e PYTHONPATH=/app \\
        -v $PWD/src:/app/src:ro -v $PWD/scripts:/app/scripts:ro \\
        kuebiko python scripts/m3b_routing_replay.py
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_DB_PATH = Path("data/run_history.db")
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_SAMPLES_PER_CELL = 5

_SQL = """
SELECT article_id, title, importance, article_type, posted_channel, routing_rule_id,
       severity, severity_basis, jp, relevant
FROM (
    SELECT a.article_id, a.title, a.importance, a.article_type, a.posted_channel,
           a.routing_rule_id, v.severity, v.severity_basis, v.jp, v.relevant,
           ROW_NUMBER() OVER (PARTITION BY a.article_id ORDER BY a.created_at DESC) AS rn
    FROM articles a
    JOIN article_importance_v2 v ON v.article_id = a.article_id
    WHERE a.created_at >= ? AND a.posted_channel IS NOT NULL
) t
WHERE rn = 1
"""  # noqa: S608 — 固定 SQL (バインドは since のみ)


@dataclass(frozen=True)
class ReplayRow:
    article_id: str
    title: str
    importance: str
    article_type: str
    old_channel: str
    old_rule_id: str
    severity: str
    severity_basis: str
    jp: str
    relevant: bool


def _connect(db_path: Path) -> Any:
    from src.storage.db_backend import connect as backend_connect

    con = backend_connect(db_path)
    if hasattr(con, "row_factory"):
        con.row_factory = sqlite3.Row
    return con


def fetch_rows(*, since: datetime, db_path: Path = DEFAULT_DB_PATH) -> list[ReplayRow]:
    """対象記事を取得する (read-only)。"""
    with _connect(db_path) as con:
        rows = con.execute(_SQL, (since.isoformat(),)).fetchall()
    return [
        ReplayRow(
            article_id=str(r["article_id"]),
            title=str(r["title"] or ""),
            importance=str(r["importance"] or ""),
            article_type=str(r["article_type"] or ""),
            old_channel=str(r["posted_channel"] or ""),
            old_rule_id=str(r["routing_rule_id"] or ""),
            severity=str(r["severity"] or ""),
            severity_basis=str(r["severity_basis"] or ""),
            jp=str(r["jp"] or ""),
            relevant=bool(r["relevant"]),
        )
        for r in rows
    ]


def new_channel_for(row: ReplayRow) -> tuple[str, str]:
    """M3B_RULES を評価して (channel, rule_id) を返す (未マッチは ("", ""))。"""
    from src.cti.router import get_source_quality
    from src.cti.routing_rules import evaluate_routing_rules
    from src.cti.routing_signals import RoutingSignals
    from src.cti.routing_v2 import M3B_RULES

    signals = RoutingSignals(
        source="briefing",
        importance=row.importance if row.importance in ("high", "medium", "low") else "low",  # type: ignore[arg-type]
        article_type=row.article_type,  # type: ignore[arg-type]
        severity=row.severity,
        severity_basis=row.severity_basis,
        jp=row.jp,
        relevant=row.relevant,
    )
    decision = evaluate_routing_rules(signals, get_source_quality(), {}, rules=M3B_RULES)
    if decision is None:
        return "", ""
    return decision.channel, decision.rule_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES_PER_CELL)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args()

    since = datetime.now(UTC) - timedelta(days=args.days)
    rows = fetch_rows(since=since, db_path=args.db_path)
    print(f"対象記事: {len(rows)} 件 (直近 {args.days} 日、article_importance_v2 記録済み)")
    if not rows:
        return

    crosstab: dict[tuple[str, str], int] = defaultdict(int)
    samples: dict[tuple[str, str], list[str]] = defaultdict(list)
    would_be_alert_low = 0

    for row in rows:
        new_channel, _new_rule_id = new_channel_for(row)
        cell = (row.old_channel, new_channel)
        crosstab[cell] += 1
        if row.old_channel != new_channel and len(samples[cell]) < args.samples:
            samples[cell].append(row.title)
        # publish._resolve_channel は importance=low の alert を default へ降格する
        # (衛生ゲート)。new_channel='alert' かつ importance='low' は「shadow 上は alert
        # だが本番導入時は降格される」件数として別途数える。
        if new_channel == "alert" and row.importance == "low":
            would_be_alert_low += 1

    print("\n旧 (v12) × 新 (M3B_RULES) チャンネルの件数:")
    print(f"{'old_channel':<16} {'new_channel':<16} {'count':>6}")
    for (old_ch, new_ch), count in sorted(crosstab.items(), key=lambda kv: -kv[1]):
        print(f"{old_ch or '(none)':<16} {new_ch or '(none)':<16} {count:>6}")

    print(f"\n食い違ったセルのサンプル (各最大 {args.samples}件):")
    for cell, titles in samples.items():
        old_ch, new_ch = cell
        print(f"\n  {old_ch or '(none)'} -> {new_ch or '(none)'}:")
        for t in titles:
            print(f"    - {t}")

    print(
        f"\nnew_channel='alert' だが importance='low' (本番導入時に publish._resolve_channel "
        f"が default へ降格する件数): {would_be_alert_low}"
    )


if __name__ == "__main__":
    main()
