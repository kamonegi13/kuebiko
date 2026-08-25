"""群化ロジックを変えたときに、**既に作られた事象を作り直す** (遡及再群化)。

毎時ジョブは「まだどの事象にも属していない記事」だけを候補にする
(``existing_member_article_ids`` による最後の関門)。そのため群化の判定を直しても
**既存の事象は分裂したまま残る**。ここでは対象の事象をいったん解体して記事を解放し、
毎時と同じ経路 (``run_eventnews_window``) で組み直す。

2026-08-25 の用途: 頻出ガードが CVE を結合信号から外していたため、大きく報じられた
事案ほど分裂していた (CVE-2026-68820 が 26 事象、CVE-2026-73570 が 17 事象)。

**既定は dry-run**。``--apply`` を付けたときだけ削除する。削除の前に必ず
``pg_dump -t event_items -t event_item_members -t event_item_versions`` を取ること。

実行例 (container 内):
  docker compose exec kuebiko python scripts/eventnews_regroup.py --cve-over-cap
  docker compose exec kuebiko python scripts/eventnews_regroup.py --cve-over-cap --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.eventnews.models import ENTITY_FREQ_CAP, ENTITY_FREQ_WINDOW_HOURS, FOCAL_CVE_MAX
from src.storage.run_history import RunHistoryRepository
from src.ui.services.eventnews_hourly_job import run_eventnews_window

# 頻出ガードを超える CVE を含む live 事象。cap 免除の前に作られたものは、
# その CVE を結合信号に使えず分裂している可能性が高い。
_SQL_TARGETS = """
WITH live AS (
  SELECT e.id FROM event_items e
   WHERE e.origin='live' AND (e.merged_into IS NULL OR e.merged_into='')
),
over_cap AS (
  SELECT value FROM article_entities
   WHERE entity_type='cve' AND datetime(created_at) >= datetime(?)
   GROUP BY value HAVING COUNT(DISTINCT article_id) > ?
)
SELECT DISTINCT l.id FROM live l
  JOIN event_item_members m ON m.item_id = l.id
  JOIN article_entities ae ON ae.article_id = m.article_id AND ae.entity_type='cve'
  JOIN over_cap oc ON oc.value = ae.value
"""


# 主題として同じ CVE を持つ事象が複数ある = 閾値緩和で合流しうる母集団。
# ``--cve-over-cap`` (頻出ガード修正用) より広い。主題の定義は grouping と同じで
# 「その記事が CVE を FOCAL_CVE_MAX 個以下しか持たない」= 一括アドバイザリでない。
_SQL_FOCAL_TARGETS = """
WITH live AS (
  SELECT e.id FROM event_items e
   WHERE e.origin='live' AND (e.merged_into IS NULL OR e.merged_into='')
),
focal AS (
  SELECT article_id FROM article_entities WHERE entity_type='cve'
   GROUP BY article_id HAVING COUNT(*) <= ?
),
ev_cve AS (
  SELECT DISTINCT l.id AS item_id, ae.value AS cve
    FROM live l
    JOIN event_item_members m ON m.item_id = l.id
    JOIN focal f ON f.article_id = m.article_id
    JOIN article_entities ae ON ae.article_id = m.article_id AND ae.entity_type='cve'
),
shared AS (
  SELECT cve FROM ev_cve GROUP BY cve HAVING COUNT(DISTINCT item_id) >= 2
)
SELECT DISTINCT ec.item_id FROM ev_cve ec JOIN shared s ON s.cve = ec.cve
"""


def _focal_targets(repo: RunHistoryRepository) -> list[str]:
    with repo._connect() as conn:  # noqa: SLF001 — 保守スクリプト
        rows = conn.execute(_SQL_FOCAL_TARGETS, (FOCAL_CVE_MAX,)).fetchall()
    return [str(r[0]) for r in rows]


def _targets(repo: RunHistoryRepository, window_hours: int) -> list[str]:
    from datetime import UTC, datetime, timedelta

    since = (datetime.now(UTC) - timedelta(hours=window_hours)).isoformat()
    with repo._connect() as conn:  # noqa: SLF001 — 保守スクリプト
        rows = conn.execute(_SQL_TARGETS, (since, ENTITY_FREQ_CAP)).fetchall()
    return [str(r[0]) for r in rows]


def _dissolve(repo: RunHistoryRepository, item_ids: list[str]) -> dict[str, int]:
    """対象事象を解体し、構成記事を未所属に戻す。メモ (event_item_notes) は消さない。"""
    if not item_ids:
        return {"items": 0, "members": 0, "versions": 0}
    placeholders = ",".join("?" for _ in item_ids)
    with repo._connect() as conn:  # noqa: SLF001
        counts = {
            "versions": conn.execute(  # noqa: S608 — placeholders のみ
                f"SELECT COUNT(*) FROM event_item_versions WHERE item_id IN ({placeholders})",
                item_ids,
            ).fetchone()[0],
            "members": conn.execute(  # noqa: S608
                f"SELECT COUNT(*) FROM event_item_members WHERE item_id IN ({placeholders})",
                item_ids,
            ).fetchone()[0],
            "items": len(item_ids),
        }
        for table, column in (
            ("event_item_versions", "item_id"),
            ("event_item_members", "item_id"),
            ("event_items", "id"),
        ):
            conn.execute(  # noqa: S608 — テーブル名は定数、値は placeholders
                f"DELETE FROM {table} WHERE {column} IN ({placeholders})", item_ids
            )
        conn.commit()
    return {k: int(v) for k, v in counts.items()}


async def _main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--cve-over-cap",
        action="store_true",
        help="頻出ガードを超える CVE を含む事象を対象にする",
    )
    ap.add_argument(
        "--focal-cve-shared",
        action="store_true",
        help="同じ CVE を主題として持つ事象が複数ある場合、それらを対象にする",
    )
    ap.add_argument("--days", type=int, default=30, help="組み直しの遡及日数")
    ap.add_argument("--apply", action="store_true", help="実際に削除して組み直す (既定は dry-run)")
    args = ap.parse_args()

    if not (args.cve_over_cap or args.focal_cve_shared):
        ap.error("対象の指定が必要です (--cve-over-cap / --focal-cve-shared)")

    repo = RunHistoryRepository()
    targets: list[str] = []
    if args.cve_over_cap:
        targets += _targets(repo, ENTITY_FREQ_WINDOW_HOURS)
    if args.focal_cve_shared:
        targets += _focal_targets(repo)
    targets = list(dict.fromkeys(targets))
    print(f"対象の事象: {len(targets)} 件", flush=True)

    if not args.apply:
        print("dry-run — 削除も組み直しもしていません (--apply で実行)", flush=True)
        return

    t0 = time.monotonic()
    removed = _dissolve(repo, targets)
    print(f"解体: {removed} ({time.monotonic() - t0:.0f}s)", flush=True)

    t1 = time.monotonic()
    result = await run_eventnews_window(lookback_hours=args.days * 24, generate=False)
    print(f"組み直し: {result} ({time.monotonic() - t1:.0f}s)", flush=True)


if __name__ == "__main__":
    asyncio.run(_main())
