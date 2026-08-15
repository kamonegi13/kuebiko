#!/usr/bin/env python3
"""victim_sector_canonical を最新 yaml で再正規化 (PG/sqlite 両対応・dry-run 既定)。

config/cti/victim_sectors.yaml に alias/canonical を追加した後、``victim_sector_canonical=
'uncategorized'`` だが raw 値が新 alias に match する既存 record を正しい canonical に
更新する (**additive only** = 既に分類済みの record は触らず、純粋に救済のみ)。

Usage (production = PG、コンテナ内で DATABASE_URL を解決):
    docker exec kuebiko /app/.venv/bin/python3 \\
        /app/scripts/renormalize_victim_sectors.py            # dry-run (集計のみ)
    docker exec kuebiko /app/.venv/bin/python3 \\
        /app/scripts/renormalize_victim_sectors.py --apply    # UPDATE 実行
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.cti.taxonomy_normalizer import load_normalizer  # noqa: E402
from src.storage.db_backend import connect  # noqa: E402


def main(apply: bool) -> int:
    normalizer = load_normalizer()
    con = connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT id, victim_sector_raw FROM articles "
            "WHERE victim_sector_canonical='uncategorized' "
            "AND victim_sector_raw IS NOT NULL AND victim_sector_raw != ''"
        )
        rows = cur.fetchall()

        print(f"=== uncategorized で raw を持つ {len(rows)} 件を再評価 ===")
        recovered: Counter[str] = Counter()
        updates: list[tuple[str, int]] = []
        for aid, raw in rows:
            new_canonical, _ = normalizer.normalize_sector(str(raw))
            if new_canonical and new_canonical != "uncategorized":
                updates.append((new_canonical, int(aid)))
                recovered[new_canonical] += 1

        print(f"  救済可能: {len(updates)} 件 (残 uncategorized: {len(rows) - len(updates)})")
        for c, n in recovered.most_common(25):
            print(f"    {c:<20} {n}")

        if not apply:
            print("\n[dry-run] 書き込みなし。--apply で additive UPDATE を実行する。")
            return 0
        for new_canonical, aid in updates:
            cur.execute(
                "UPDATE articles SET victim_sector_canonical=? WHERE id=?",
                (new_canonical, aid),
            )
        con.commit()
        print(f"\n[applied] {len(updates)} 件を救済した。")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(apply="--apply" in sys.argv))
