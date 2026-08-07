#!/usr/bin/env python3
"""スケジュール欠落期間の weekly status_synthesis を射影のみで再生成する (2026-07-16)。

背景: 7/6-7/12 週の weekly がスケジュール移設の遷移窓で欠落 (retrospect の synthesis
パネル null)。当該週の revision は台帳に揃っているため、**射影専用経路**
(build_projection_estimate — 評価なし・台帳書込なし) で narrative を再生成して
status_synthesis に upsert する。Discord 配信と forecast snapshot は行わない
(配信は当時性がなく、snapshot は次回定時 run が正史を刻む)。

Usage (production PG、コンテナ内):
    docker cp scripts/backfill_weekly_synthesis.py kuebiko:/tmp/bws.py
    docker exec kuebiko python /tmp/bws.py --as-of 2026-07-13T02:45:00+09:00
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO if (_REPO / "src").is_dir() else Path("/app")))

from src.config_loader import load_app_config  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.synthesis.grounded.pipeline import generate_grounded_synthesis  # noqa: E402
from src.tools.model_tiers import Step, build_llm_for  # noqa: E402

# 欠落した 7/6-7/12 週を解決する「当時の run 時刻」相当 (月曜 02:45 JST)
_DEFAULT_AS_OF = "2026-07-13T02:45:00+09:00"


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", default=_DEFAULT_AS_OF, help="期間解決に使う当時の時刻 (ISO)")
    parser.add_argument("--period", default="weekly", choices=["weekly", "monthly"])
    parser.add_argument("--dry-run", action="store_true", help="生成のみ (upsert しない)")
    args = parser.parse_args()

    as_of = datetime.fromisoformat(args.as_of)
    config = load_app_config()
    llm = build_llm_for(Step.SYNTHESIS_NARRATIVE, config)
    print(f"projection backfill: period={args.period} as_of={as_of.isoformat()} model={llm.model}")

    result = await generate_grounded_synthesis(
        llm=llm, period_type=args.period, now=as_of, projection_only=True
    )
    if result.record is None:
        print(f"FAILED: {result.error}")
        return 1
    rec = result.record
    print(f"generated: period {rec.period_start} .. {rec.period_end}")
    print(f"headline: {rec.headline[:160]}")
    print(f"article_count: {rec.article_count}")
    if args.dry_run:
        print("DRY-RUN: not persisted")
        return 0
    RunHistoryRepository().upsert_status_synthesis(rec)
    print("persisted to status_synthesis (Discord 配信・forecast snapshot は行わない)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
