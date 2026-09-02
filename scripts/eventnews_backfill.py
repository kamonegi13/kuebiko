"""事象ニュースの遡及構築 (バックフィル)。

毎時運用は「新着記事を既存アイテムへ足す」だけなので、**運用開始より前の記事は
永久に事象化されない**。過去 N 日を遡って群化し、まだ版を持たないアイテムに
最終状態で 1 回だけ生成する。

**取得は毎時ジョブと完全に共有する** (``run_eventnews_window``)。2026-08-24 の
不発は「評価と本番で entity の引き方が違った」ことが原因だったので、
遡及幅だけを変えて他の経路を分岐させない。

生成は逐次再生しない: 状態遷移ごとに生成すると、読み手に見えない中間版へ
LLM 時間を費やすことになる。過去の「更新の履歴」は事後には価値が無く、
必要なのは今読める最終形だけ。

実行例 (container 内、本番 PG へ書く):
  docker compose exec kuebiko python scripts/eventnews_backfill.py --days 30 --group-only
  docker compose exec kuebiko python scripts/eventnews_backfill.py --generate --limit 50
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config_loader import load_app_config
from src.eventnews.runner import generate_pending
from src.storage.run_history import RunHistoryRepository
from src.tools.llm_client import LLMClient
from src.tools.model_tiers import Step, build_llm_for
from src.ui.services.eventnews_hourly_job import (
    pending_items,
    run_eventnews_window,
)


async def _main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30, help="群化の遡及日数")
    ap.add_argument("--group-only", action="store_true", help="群化のみ (生成しない)")
    ap.add_argument("--generate", action="store_true", help="未生成アイテムの生成のみ行う")
    ap.add_argument("--limit", type=int, default=None, help="生成する最大件数")
    ap.add_argument(
        "--sleep", type=float, default=0.0, help="生成 1 件ごとの待機秒 (他ジョブとの競合緩和)"
    )
    args = ap.parse_args()

    repo = RunHistoryRepository()

    if not args.generate:
        t0 = time.monotonic()
        result = await run_eventnews_window(lookback_hours=args.days * 24, generate=False)
        print(f"群化: {result} ({time.monotonic() - t0:.0f}s)", flush=True)

    pending = pending_items(repo)
    print(f"未生成のアイテム: {len(pending)} 件", flush=True)
    if args.group_only or not pending:
        return

    config = load_app_config()

    def _llm() -> LLMClient:
        return build_llm_for(Step.EVENT_NEWS, config)

    last = time.monotonic()

    def _progress(i: int, total: int, item_id: str) -> None:
        nonlocal last
        now = time.monotonic()
        print(f"  [{i}/{total}] {item_id} (前件 {now - last:.0f}s)", flush=True)
        last = now
        if args.sleep > 0:
            time.sleep(args.sleep)

    t0 = time.monotonic()
    stats = await generate_pending(repo, pending, _llm, limit=args.limit, on_progress=_progress)
    print(
        f"生成 {stats.generated} / 素材不足で skip {stats.skipped} / 失敗 {stats.failed}"
        f" (対象 {stats.attempted}, {time.monotonic() - t0:.0f}s)",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(_main())
