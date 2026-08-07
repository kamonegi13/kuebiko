"""PMESII 軸の ground truth 作成用 article sample 出力。

random 100 件の article を export し、 user が manual で 8 軸 label を付与する。
出力 JSON は scripts/evaluate_pmesii.py で F1 score 計算に使用。

Usage (host から):
    uv run --env-file .env python scripts/sample_pmesii_for_label.py \\
        --n 100 --out data/pmesii_eval/ground_truth.json

出力 JSON 構造:
[
  {
    "article_id": "rss:...",
    "title": "...",
    "summary": "...",
    "body_preview": "...",  // 先頭 800 char
    "current_axes": ["I-cyber", "P"],  // 現状 reclassify 結果 (参考、 上書き OK)
    "expected_axes": null  // user が手動 label (8 軸の sorted list を入れる)
  },
  ...
]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.storage.run_history import RunHistoryRepository

PMESII_COLS = [
    ("P", "pmesii_p"),
    ("M", "pmesii_m"),
    ("E", "pmesii_e"),
    ("S", "pmesii_s"),
    ("I-infra", "pmesii_i_infra"),
    ("I-cyber", "pmesii_i_cyber"),
    ("P-env", "pmesii_p_env"),
    ("T", "pmesii_t"),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=100, help="サンプル件数")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/pmesii_eval/ground_truth.json"),
        help="出力 JSON path",
    )
    args = parser.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)

    repo = RunHistoryRepository()
    cols = ", ".join(c for _, c in PMESII_COLS)
    with repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            f"""
            SELECT article_id, title, summary, body,
                   {cols}
              FROM articles
             WHERE status = 'posted' AND body IS NOT NULL AND LENGTH(body) > 300
             ORDER BY random()
             LIMIT %s
            """,  # noqa: S608
            (args.n,),
        ).fetchall()

    out: list[dict] = []
    for row in rows:
        current = [name for name, col in PMESII_COLS if row[col] == 1]
        out.append({
            "article_id": row["article_id"],
            "title": row["title"] or "",
            "summary": (row["summary"] or "")[:300],
            "body_preview": (row["body"] or "")[:800],
            "current_axes": sorted(current),
            "expected_axes": None,
        })

    with args.out.open("w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"sampled {len(out)} articles → {args.out}", file=sys.stderr)
    print("次の手順:", file=sys.stderr)
    print(f"  1. {args.out} を editor で開く", file=sys.stderr)
    print('  2. 各 article の "expected_axes" に 8 軸のうち該当を JSON list で記入', file=sys.stderr)
    print('     (例: ["I-cyber", "P"])', file=sys.stderr)
    print("  3. scripts/evaluate_pmesii.py で F1 score を計算", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
