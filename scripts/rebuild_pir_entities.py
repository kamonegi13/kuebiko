#!/usr/bin/env python3
"""PIR-persist backfill: article×PIR を entity_type='pir' に永続化 (full rebuild)。

通常は pir-daily-focus (日次06:30) が rebuild するが、初回 / 手動用。dry-run 既定。

Usage (production = PG、コンテナ内で DATABASE_URL を解決):
    docker exec kuebiko /app/.venv/bin/python3 \\
        /app/scripts/rebuild_pir_entities.py            # dry-run
    docker exec kuebiko /app/.venv/bin/python3 \\
        /app/scripts/rebuild_pir_entities.py --apply    # 置換
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.pir.persist import rebuild_pir_entities  # noqa: E402


def main(apply: bool) -> int:
    stats = rebuild_pir_entities(dry_run=not apply)
    label = "applied" if apply else "dry-run"
    print(f"[{label}] {stats}")
    if not apply:
        print("--apply で entity_type='pir' を置換する。")
    return 0


if __name__ == "__main__":
    sys.exit(main(apply="--apply" in sys.argv))
