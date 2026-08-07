#!/usr/bin/env python3
"""過去記事の body_ja を手動で一括翻訳する (毎時バックログジョブの手動加速版)。

毎時ジョブ (body-translate-backlog、40 件/時) だけだと過去分 ~22k 件の消化に
1-2 ヶ月かかる。マシンがアイドルな時間帯 (週末・夜間前) にこのスクリプトを回せば
実測 ~19 秒/件で先行消化できる。runner は毎時ジョブと同一 (resume 自由・重複なし —
body_ja IS NULL が消化条件なので中断してもやり直しが効く)。

Usage:
    docker exec kuebiko /app/.venv/bin/python3 -m scripts.backfill_body_ja \\
        --max-articles 500
    # ホスト直実行も可: uv run python -m scripts.backfill_body_ja --max-articles 500
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# repo root を sys.path に
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.logging_config import get_logger  # noqa: E402
from src.ui.services.body_translate_backlog import run_body_translate_backlog  # noqa: E402

_log = get_logger(__name__)


async def main() -> int:
    parser = argparse.ArgumentParser(description="body_ja 手動 backfill")
    parser.add_argument(
        "--max-articles",
        type=int,
        default=200,
        help="この実行で翻訳する最大件数 (default 200 ≈ 65 分)",
    )
    args = parser.parse_args()

    stats = await run_body_translate_backlog(
        batch_limit=args.max_articles,
        # 手動実行は時間予算で切らない (件数 cap のみ)
        time_budget_seconds=float("inf"),
    )
    _log.info("backfill_body_ja_done", **stats)
    print(f"translated={stats['translated']} failed={stats['failed']} picked={stats['picked']}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
