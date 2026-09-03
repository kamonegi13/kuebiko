"""triage のドリフト検知 — 凍結 goldset を現在の 26B に通し、day-0 と比べる。

⭐ day-0 (2026-09-03) の実測: 26B(当時) vs Sonnet 55.3% (全て 26B が高めに付ける
方向・見逃し方向 0 件)。26B vs 過去の保存値は 40% — 保存済みラベルは現在の体制を
代表しない。これが凍結 goldset の存在理由。

比べるのは **day-0 の 26B 自身の答え** (now_26b)。基準からの移動 = ドリフト。
Sonnet 列は参照 (運用点の違いの記録) で、合否には使わない。

実行: docker exec kuebiko python /app/scripts/triage_drift_check.py [--threshold 0.15]
"""

import argparse
import asyncio
import json
import sys
from collections import Counter

sys.path.insert(0, "/app")

from src.config_loader import load_app_config
from src.tools.article_triage import TriageDecision
from src.tools.model_tiers import Step, build_llm_for

_GOLDSET = "/app/data/eval/triage_goldset.json"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=0.15, help="警告するドリフト率")
    ap.add_argument("--goldset", default=_GOLDSET)
    args = ap.parse_args()

    with open(args.goldset, encoding="utf-8") as f:
        rows = json.load(f)["rows"]
    base = [r for r in rows if r.get("now_26b")]
    llm = build_llm_for(Step.TRIAGE, load_app_config())
    moved = 0
    conf: Counter[tuple[str, str]] = Counter()
    danger = 0
    for i, r in enumerate(base, 1):
        try:
            v = await llm.generate_structured(
                prompt=r["prompt"], schema=TriageDecision, temperature=0.0, think=False
            )
            now = v.importance
        except Exception:
            continue
        if now != r["now_26b"]:
            moved += 1
            conf[(r["now_26b"], now)] += 1
            if r["now_26b"] in ("high", "medium") and now == "low":
                danger += 1
        if i % 30 == 0:
            print(f"  {i}/{len(base)}", flush=True)
    rate = moved / max(1, len(base))
    print(f"\nドリフト: {moved}/{len(base)} = {rate:.1%} (警告閾値 {args.threshold:.0%})")
    for (a, b), n in conf.most_common(6):
        print(f"  day-0 {a} → 現在 {b}: {n}")
    print(f"⚠ 降格方向 (high/medium → low): {danger}")
    if rate >= args.threshold:
        print("⚠ ドリフトが閾値超え — プロンプト/PIR/モデルの直近変更を確認すること")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
