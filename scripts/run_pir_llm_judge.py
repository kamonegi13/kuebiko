"""概念 PIR の LLM 主題判定を手動実行する (backfill / 検証用)。

夜間バッチ (pir-entity-rebuild 前段) と同一経路 (judge_pending)。差は cap のみ。

    # 対象と backlog の確認 (LLM を呼ばない)
    docker exec -e PYTHONPATH=/app -w /app kuebiko \
        python scripts/run_pir_llm_judge.py --dry-run

    # backfill (窓いっぱい判定 → 続けて rebuild を打つこと)
    docker exec -e PYTHONPATH=/app -w /app kuebiko \
        python scripts/run_pir_llm_judge.py --cap 5000 --concurrency 2
"""

from __future__ import annotations

import argparse
import asyncio
import json


def main() -> None:
    parser = argparse.ArgumentParser(description="PIR LLM 主題判定バッチ")
    parser.add_argument("--window-days", type=int, default=90)
    parser.add_argument("--cap", type=int, default=5000, help="1 実行の判定上限")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument(
        "--dry-run", action="store_true", help="対象数のみ出力 (LLM を呼ばず書き込まない)"
    )
    args = parser.parse_args()

    from src.pir.llm_judge import judge_pending

    stats = asyncio.run(
        judge_pending(
            window_days=args.window_days,
            max_judgments=args.cap,
            concurrency=args.concurrency,
            dry_run=args.dry_run,
        )
    )
    print(json.dumps(stats, ensure_ascii=False))
    if not args.dry_run and stats.get("judged"):
        print("→ 反映には rebuild_pir_entities.py --apply を実行すること")


if __name__ == "__main__":
    main()
