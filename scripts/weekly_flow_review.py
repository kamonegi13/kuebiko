#!/usr/bin/env python3
"""情報フロー週次レビュー (2026-08-15 の alert 再設計の効果測定)。

2026-08-15 に実施した変更 (案 D 限定 / R2e 速報級 / R2f 早期警戒 / R2g 緊急指令 /
advisory 除外 / CVSS 補給) の効果を 1 コマンドで測定する。読み取り専用。

    docker cp scripts/weekly_flow_review.py kuebiko:/tmp/ && \\
        docker exec kuebiko python /tmp/weekly_flow_review.py [--days 7]

測るもの:
  1. alert 件数/日 とルール別内訳 (R2e/R2f/R2g の実発火 = 追加側の実測)
  2. 初報 watch → 続報 alert の昇格実例 (「重要なら続報で CVSS が付く」経路の実証)
  3. CVSS 滞留の解消状況 (nvd-cvss-refresh の実効)
  4. Grok テーマ別の収穫 (9 タスク体制の実り)
"""

from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.storage.db_backend import connect, translate_sql  # noqa: E402


def section(title: str) -> None:
    print(f"\n{'=' * 8} {title} {'=' * 8}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    win = f"-{args.days} days"
    conn = connect()

    section(f"1. alert 件数とルール別内訳 (直近 {args.days} 日)")
    rows = conn.execute(
        translate_sql(
            "SELECT routing_reason, COUNT(*) AS n FROM articles"
            " WHERE posted_channel='alert' AND created_at > datetime('now', ?)"
            " GROUP BY routing_reason ORDER BY n DESC"
        ),
        (win,),
    ).fetchall()
    total = sum(int(r["n"]) for r in rows)
    print(f"  alert 合計 {total} 件 ({total / args.days:.1f} 件/日)  ※目標帯 7-8 件/日")
    for r in rows:
        print(f"    {str(r['routing_reason'] or '-'):52} {int(r['n']):4}")

    section("2. 初報 watch → 続報 alert の昇格実例")
    rows = conn.execute(
        translate_sql(
            "SELECT e.value AS cve,"
            " MIN(CASE WHEN a.posted_channel='watch' THEN a.created_at END) AS first_watch,"
            " MIN(CASE WHEN a.posted_channel='alert' THEN a.created_at END) AS first_alert"
            " FROM article_entities e JOIN articles a ON a.article_id=e.article_id"
            " WHERE e.entity_type='cve' AND a.created_at > datetime('now', ?)"
            " GROUP BY e.value"
            " HAVING MIN(CASE WHEN a.posted_channel='watch' THEN a.created_at END) IS NOT NULL"
            " AND MIN(CASE WHEN a.posted_channel='alert' THEN a.created_at END) IS NOT NULL"
        ),
        (win,),
    ).fetchall()
    promoted = [r for r in rows if str(r["first_watch"]) < str(r["first_alert"])]
    print(f"  watch 初報 → 後日 alert 昇格: {len(promoted)} CVE")
    for r in promoted[:8]:
        print(
            f"    {r['cve']}: watch {str(r['first_watch'])[:10]}"
            f" → alert {str(r['first_alert'])[:10]}"
        )

    section("3. CVSS 滞留 (nvd-cvss-refresh の実効)")
    from src.storage.run_history import RunHistoryRepository
    from src.tools.nvd_client import get_cvss

    repo = RunHistoryRepository()
    recent = repo.recent_cve_values(days=30, limit=2000)
    missing = sum(1 for c in recent if get_cvss(c) is None)
    print(
        f"  直近 30 日の CVE {len(recent)} 件 / CVSS 未取得 {missing} 件"
        f" (充足率 {100 * (len(recent) - missing) / max(len(recent), 1):.0f}%)"
    )

    section(f"4. Grok テーマ別の収穫 (直近 {args.days} 日)")
    rows = conn.execute(
        translate_sql(
            "SELECT COALESCE(NULLIF(SUBSTR(a.routing_reason, 1, 0), ''), a.posted_channel) AS ch,"
            " a.status, COUNT(*) AS n FROM articles a"
            " WHERE a.feed_title='Grok' AND a.created_at > datetime('now', ?)"
            " GROUP BY 1, 2 ORDER BY n DESC"
        ),
        (win,),
    ).fetchall()
    theme_rows = conn.execute(
        translate_sql(
            "SELECT l.line FROM run_logs l JOIN runs r ON r.id = l.run_id"
            " WHERE r.pipeline='grok-briefing' AND r.started_at > datetime('now', ?)"
            " AND l.line LIKE ?"
        ),
        (win, '%"theme"%'),
    ).fetchall()
    import json as _json

    themes: collections.Counter[str] = collections.Counter()
    for tr in theme_rows:
        try:
            t = _json.loads(tr["line"]).get("theme")
            if t:
                themes[str(t)] += 1
        except Exception:  # noqa: BLE001,S112 — 集計用、壊れた行は無視
            continue
    print("  配信状況:", {f"{r['ch']}/{r['status']}": int(r["n"]) for r in rows})
    print("  テーマ別レコード観測:", dict(themes.most_common(15)) or "(なし)")


if __name__ == "__main__":
    main()
