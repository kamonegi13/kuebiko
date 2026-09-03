"""triage ドリフト検知の CLI (判定ロジックの SSoT は src/eval/triage_drift.py)。

実行: docker exec kuebiko python /app/scripts/triage_drift_check.py [--threshold 0.15]
週次自動実行は weekly-triage-drift ジョブ (日曜 04:40) が同じ実装を呼ぶ。
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, "/app")

from src.eval.triage_drift import DRIFT_WARN_RATE, GOLDSET_PATH, measure_drift


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=DRIFT_WARN_RATE)
    ap.add_argument("--goldset", default=str(GOLDSET_PATH))
    args = ap.parse_args()

    result = await measure_drift(Path(args.goldset))
    if result is None:
        print("goldset が見つからない")
        sys.exit(2)
    print(f"ドリフト: {result.moved}/{result.total} = {result.rate:.1%}")
    print(f"警告閾値 {args.threshold:.0%}")
    for a, b, n in result.transitions[:6]:
        print(f"  day-0 {a} → 現在 {b}: {n}")
    print(f"⚠ 降格方向 (high/medium → low): {result.demoted}")
    if result.rate >= args.threshold:
        print("⚠ ドリフトが閾値超え — プロンプト/PIR/モデルの直近変更を確認すること")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
