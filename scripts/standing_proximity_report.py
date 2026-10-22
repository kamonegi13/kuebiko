#!/usr/bin/env python3
"""常設の問いの根拠を 直接 (日本・同盟国) / 間接 (それ以外) で数える — SELECT のみ。

2026-10-22。印の定義は ``src/cti/alliance.py`` (区分の SSoT = config/cti/countries.yaml)。
評価に使われた根拠 (``situation_evidence.assessed_at`` が窓内) を問いごとに集計する。

使い方 (DATABASE_URL が PG を指す環境):
    uv run python scripts/standing_proximity_report.py --days 30
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.assessment.standing_proximity import (  # noqa: E402
    proximity_by_question,
    window_rows_sql,
)
from src.cti.alliance import allied_nations  # noqa: E402
from src.storage.db_backend import connect  # noqa: E402
from src.storage.run_history import DEFAULT_DB_PATH  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()
    since = (datetime.now(UTC) - timedelta(days=args.days)).isoformat()
    conn = connect(DEFAULT_DB_PATH)
    try:
        fetched = conn.execute(window_rows_sql(), (since,)).fetchall()
    finally:
        conn.close()
    rows = [(str(r[0]), str(r[1]), str(r[2]), r[3]) for r in fetched]
    print(f"同盟・同志国: {sorted(c.upper() for c in allied_nations())}")
    total = {"direct": 0, "indirect": 0, "unknown": 0}
    for sid, title, counts in proximity_by_question(rows):
        n = sum(counts.values())
        print(
            f"{sid}\t{title}\t根拠{n}\t直接{counts['direct']}"
            f"\t間接{counts['indirect']}\t不明{counts['unknown']}"
        )
        for k, v in counts.items():
            total[k] += v
    print(
        f"合計\t根拠{sum(total.values())}\t直接{total['direct']}"
        f"\t間接{total['indirect']}\t不明{total['unknown']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
