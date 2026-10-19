#!/usr/bin/env python3
"""articles.original_title を dedup_seen_urls.title (取込時の元の見出し) から埋める。

既定は dry-run (件数だけ表示)。--apply で更新する。dedup_seen_urls が持つ分 (2026-05 以降) だけ。

Usage:
    docker exec kuebiko /app/.venv/bin/python3 -m scripts.backfill_original_title [--apply]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.storage.original_title_backfill import apply_backfill, count_backfillable  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="更新する (既定は dry-run)")
    args = ap.parse_args()
    repo = RunHistoryRepository()
    with repo._connect() as conn:  # noqa: SLF001 — 保守スクリプト
        n = count_backfillable(conn)
        print(f"埋められる行: {n}")
        if not args.apply:
            print("dry-run (--apply で更新)")
            return 0
        updated = apply_backfill(conn)
        print(f"更新: {updated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
