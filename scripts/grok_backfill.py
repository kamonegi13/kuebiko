#!/usr/bin/env python3
"""Grok レポートの過去分を分析専用で取り込む (一度だけの手動実行、2026-11-18)。

11-15〜11-18 は Grok の画面の自動リンク化で JSONL が読めず要約が 0 件だった
(src/grok/backfill.py)。その間のレポートの投稿を、Discord へは流さず
``articles.analysis_only='grok_backfill'`` として残す。

本番コンテナの中で 1 回ずつ流す (常駐の環境には旗を入れない)。runs に 1 行を作り、
終わったら totals を書く。1 回の上限は ``--budget`` 秒 (soft deadline)。投稿が残れば
次の実行が既読の判定で処理済みを飛ばして続きから進むので、``deferred=0`` になるまで繰り返す:

  docker exec -e GROK_BACKFILL=1 -e GROK_BACKFILL_UNTIL=2026-11-18T13:56:00+00:00 \\
      kuebiko python scripts/grok_backfill.py --budget 1500

GROK_BACKFILL_UNTIL より後に受信したメールは扱わない (修正後のレポートは通常の run が配信する)。
対象の行は ``articles.analysis_only='grok_backfill'`` で特定できる (配信はしていない)。
取り消すなら ``DELETE FROM articles WHERE analysis_only='grok_backfill'``
(既読の登録は残るので同じ投稿は再取込されない)。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.grok.backfill import grok_backfill_enabled, grok_backfill_until  # noqa: E402
from src.main import main as pipeline_main  # noqa: E402
from src.storage.run_history import RunHistoryRepository, RunRecord  # noqa: E402

PIPELINE = "grok-briefing"
RESULT_DIR = Path("data/run_results")


def _run_once(budget: float, max_articles: int) -> int:
    repo = RunHistoryRepository()
    run_id = repo.start_run(
        RunRecord(
            started_at=datetime.now(UTC),
            pipeline=PIPELINE,
            dry_run=False,
            triggered_by="manual",
            note="grok_backfill",
        )
    )
    os.environ["PIPELINE_TIME_BUDGET_SECONDS"] = str(budget)
    code = pipeline_main(
        ["--pipeline", PIPELINE, "--run-id", str(run_id), "--max-articles", str(max_articles)]
    )
    result_path = RESULT_DIR / f"{run_id}.json"
    result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
    errors = result.get("errors") or []
    repo.finish_run(
        run_id,
        status="succeeded" if code == 0 else "failed",
        finished_at=datetime.now(UTC),
        total_fetched=int(result.get("total_fetched", 0)),
        summarized=int(result.get("summarized", 0)),
        posted=int(result.get("posted", 0)),
        marked_read=int(result.get("marked_read", 0)),
        error_count=len(errors),
        note=f"grok_backfill deferred={result.get('deferred_count', 0)}",
    )
    print(
        f"run={run_id} exit={code} fetched={result.get('total_fetched', 0)} "
        f"summarized={result.get('summarized', 0)} deferred={result.get('deferred_count', 0)} "
        f"errors={len(errors)}"
    )
    return code


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=float, default=1500.0, help="1 回の上限 (秒)")
    ap.add_argument("--max-articles", type=int, default=60, help="1 回に扱うレポートの数の上限")
    args = ap.parse_args()
    if not grok_backfill_enabled():
        sys.exit("GROK_BACKFILL=1 で実行してください (通常の run と区別するため)")
    print(f"until={grok_backfill_until().isoformat()}")
    return _run_once(args.budget, args.max_articles)


if __name__ == "__main__":
    raise SystemExit(main())
