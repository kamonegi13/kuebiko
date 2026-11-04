"""S の切替で学び直す ML の一覧と、学習時の S の整合を表示する (読み取りのみ、S-SWITCH-RETRAIN)。

使い方: uv run python scripts/check_s_switch_ml.py [--models-dir config/models] [--s <新しい S>]
``--s`` 省略時は ``model_registry.RESIDENT_SLOTS["S"]``。終了コード 1 = 特徴の見落とし
(未対応の特徴量・規則表に無い JSON) または学習時の S の不一致。``unrecorded`` は警告のみ。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.tools.ml_s_dependency import (
    NOT_RETRAINED,
    RETRAIN_PLAN,
    RETRAIN_TARGETS,
    derive_dependencies,
    find_misaligned,
)
from src.tools.model_registry import RESIDENT_SLOTS


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models-dir", type=Path, default=Path("config/models"))
    ap.add_argument("--s", default=RESIDENT_SLOTS["S"], help="比べる S のモデル名")
    args = ap.parse_args()

    report = derive_dependencies(args.models_dir)
    print(f"S = {args.s}")
    print("\n[S 由来の特徴を持つ ML]")
    for d in report.s_derived():
        mark = "対象" if d.file in RETRAIN_TARGETS else "対象外"
        trained = ",".join(d.trained_models) or "(記録なし)"
        head = f"  {d.file:28s} {d.step:18s} {d.strength:6s} {mark}"
        print(f"{head} n={len(d.features):2d} 学習時={trained}")
    print("\n[対象外の理由]")
    for file, why in NOT_RETRAINED.items():
        print(f"  {file}: {why}")
    print("\n[学習スクリプト]")
    for step in RETRAIN_PLAN:
        print(f"  {step.file}: {step.trainer or '(repo 内に無い)'}")

    bad = 0
    for u in report.unmapped:
        print(f"ERROR 未対応の特徴量: {u.file} {u.feature}")
        bad += 1
    for f in report.unknown_files:
        print(f"ERROR 規則表に無い JSON: {f}")
        bad += 1
    for m in find_misaligned(report, args.s):
        level = "ERROR" if m.reason == "mismatch" else "WARN"
        print(f"{level} {m.reason}: {m.file} {m.step} 学習時={','.join(m.trained_models) or '-'}")
        bad += m.reason == "mismatch"
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
