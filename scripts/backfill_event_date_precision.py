"""articles.event_date_precision の埋め直し (既定は dry-run、--apply で書く)。"""

from __future__ import annotations

import argparse

from src.storage.event_date_precision_backfill import build_backfill_statements


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="UPDATE を実行する (既定は文を表示のみ)")
    args = ap.parse_args()
    stmts = build_backfill_statements()
    if not args.apply:
        for s in stmts:
            print(s + ";")
        return
    from src.storage.db_backend import transaction  # 遅延 import (dry-run は DB 不要)

    with transaction() as conn:
        for s in stmts:
            cur = conn.execute(s)
            print(f"{cur.rowcount} rows <- {s[:70]}")


if __name__ == "__main__":
    main()
