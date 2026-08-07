"""ground truth と DB の現状 PMESII 分類を比較し F1 score を計算。

Phase 2 評価基盤。 軸別 precision / recall / F1 + 全体 micro-F1 を出力。

Usage:
    uv run --env-file .env python scripts/evaluate_pmesii.py \\
        --ground-truth data/pmesii_eval/ground_truth.json

出力例:
    axis      P    M    E    S   I-infra I-cyber P-env  T   micro-avg
    precision 0.81 0.85 0.78 0.65   0.72    0.92   0.55 0.40  0.78
    recall    0.75 0.88 0.81 0.62   0.65    0.95   0.50 0.30  0.76
    F1        0.78 0.86 0.79 0.63   0.68    0.93   0.52 0.34  0.77

注意:
    expected_axes が null の article は skip (= まだ label されていない)。
    すべて null なら 0 件で評価。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.storage.run_history import RunHistoryRepository

PMESII_AXES = ["P", "M", "E", "S", "I-infra", "I-cyber", "P-env", "T"]
PMESII_COLS = {
    "P": "pmesii_p",
    "M": "pmesii_m",
    "E": "pmesii_e",
    "S": "pmesii_s",
    "I-infra": "pmesii_i_infra",
    "I-cyber": "pmesii_i_cyber",
    "P-env": "pmesii_p_env",
    "T": "pmesii_t",
}


def f1_for_axis(
    expected: list[set[str]], actual: list[set[str]], axis: str,
) -> tuple[float, float, float, int, int]:
    """1 軸の precision / recall / F1。 戻り値: (P, R, F1, support_expected, support_actual)。"""
    tp = sum(1 for e, a in zip(expected, actual, strict=True) if axis in e and axis in a)
    fp = sum(1 for e, a in zip(expected, actual, strict=True) if axis not in e and axis in a)
    fn = sum(1 for e, a in zip(expected, actual, strict=True) if axis in e and axis not in a)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall) / (precision + recall) if (precision + recall) else 0.0
    return precision, recall, f1, tp + fn, tp + fp


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ground-truth",
        type=Path,
        default=Path("data/pmesii_eval/ground_truth.json"),
    )
    args = parser.parse_args()

    if not args.ground_truth.exists():
        print(f"❌ ground truth not found: {args.ground_truth}", file=sys.stderr)
        print("  先に sample_pmesii_for_label.py を実行してください", file=sys.stderr)
        return 1

    with args.ground_truth.open(encoding="utf-8") as f:
        gt = json.load(f)

    labeled = [item for item in gt if item.get("expected_axes") is not None]
    print(f"evaluate: {len(labeled)}/{len(gt)} labeled", file=sys.stderr)
    if not labeled:
        print("❌ expected_axes がすべて null。 まず手動 label してください", file=sys.stderr)
        return 1

    # DB から現状の PMESII を取得
    repo = RunHistoryRepository()
    article_ids = [it["article_id"] for it in labeled]
    placeholders = ",".join(["%s"] * len(article_ids))
    cols = ", ".join(PMESII_COLS.values())
    with repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            f"SELECT article_id, {cols} FROM articles WHERE article_id IN ({placeholders})",  # noqa: S608
            article_ids,
        ).fetchall()

    actual_by_id: dict[str, set[str]] = {}
    for row in rows:
        actual: set[str] = set()
        for axis, col in PMESII_COLS.items():
            if row[col] == 1:
                actual.add(axis)
        actual_by_id[row["article_id"]] = actual

    expected_list: list[set[str]] = []
    actual_list: list[set[str]] = []
    for it in labeled:
        expected_list.append(set(it["expected_axes"]))
        actual_list.append(actual_by_id.get(it["article_id"], set()))

    # 軸別 + micro-avg
    print(f"\n{'axis':<10} {'P':>6s} {'R':>6s} {'F1':>6s} {'sup_exp':>8s} {'sup_act':>8s}")
    print("-" * 50)
    micro_tp = micro_fp = micro_fn = 0
    for axis in PMESII_AXES:
        p, r, f, sup_e, sup_a = f1_for_axis(expected_list, actual_list, axis)
        print(f"{axis:<10} {p:6.2f} {r:6.2f} {f:6.2f} {sup_e:>8d} {sup_a:>8d}")
        for e, a in zip(expected_list, actual_list, strict=True):
            if axis in e and axis in a:
                micro_tp += 1
            elif axis not in e and axis in a:
                micro_fp += 1
            elif axis in e and axis not in a:
                micro_fn += 1

    micro_p = micro_tp / (micro_tp + micro_fp) if (micro_tp + micro_fp) else 0.0
    micro_r = micro_tp / (micro_tp + micro_fn) if (micro_tp + micro_fn) else 0.0
    micro_f1 = (
        (2 * micro_p * micro_r) / (micro_p + micro_r) if (micro_p + micro_r) else 0.0
    )
    print("-" * 50)
    print(f"{'micro-avg':<10} {micro_p:6.2f} {micro_r:6.2f} {micro_f1:6.2f}")

    # 軸別 confusion 分析 (top 5 misclassified pairs)
    print("\n=== 誤分類サンプル (top 5) ===")
    mismatches = []
    for it, exp, act in zip(labeled, expected_list, actual_list, strict=True):
        if exp != act:
            mismatches.append({
                "article_id": it["article_id"],
                "title": it["title"][:80],
                "expected": sorted(exp),
                "actual": sorted(act),
            })
    for m in mismatches[:5]:
        print(f"  - {m['title']}")
        print(f"      expected: {m['expected']}")
        print(f"      actual:   {m['actual']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
