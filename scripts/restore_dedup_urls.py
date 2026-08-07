"""recovered_urls.txt から dedup_seen_urls table を復元。

DB 破損後の fresh schema に、recovery script で抽出した article URL を
url_hash 付きで bulk insert する。一度 seen 化された URL は将来再 fetch
されないため、重複ノイズを防ぐ。

Usage (container 内):
    docker exec kuebiko /app/.venv/bin/python3 /tmp/restore_dedup_urls.py
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from src.tools.url_normalizer import url_hash

DB_PATH = Path("/app/data/run_history.db")
URLS_PATH = Path("/app/data/recovery/article_urls.txt")
BATCH_SIZE = 1000


def main() -> int:
    if not URLS_PATH.exists():
        print(f"❌ {URLS_PATH} not found")
        return 1
    if not DB_PATH.exists():
        print(f"❌ {DB_PATH} not found")
        return 1

    now = datetime.now(UTC).isoformat()

    # tab-separated: count\turl
    rows: list[tuple[str, str, str, str, int]] = []
    seen_hashes: set[str] = set()
    skipped = 0
    with URLS_PATH.open(encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t", 1)
            if len(parts) != 2:
                continue
            count_str, url = parts
            try:
                count = int(count_str)
            except ValueError:
                continue
            # 長すぎる URL は drop
            if len(url) > 2000:
                skipped += 1
                continue
            try:
                h = url_hash(url)
            except Exception:  # noqa: BLE001
                skipped += 1
                continue
            if h in seen_hashes:
                # 既に追加済 (同じ正規化 URL の異なる raw)
                continue
            seen_hashes.add(h)
            rows.append((h, url, now, now, max(count, 1)))

    print(f"Prepared {len(rows)} rows ({skipped} skipped)")

    # Bulk insert
    conn = sqlite3.connect(DB_PATH)
    try:
        # 既存件数確認
        cur = conn.execute("SELECT COUNT(*) FROM dedup_seen_urls")
        before = cur.fetchone()[0]
        print(f"dedup_seen_urls before: {before}")

        inserted = 0
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i:i + BATCH_SIZE]
            conn.executemany(
                """
                INSERT OR IGNORE INTO dedup_seen_urls
                (url_hash, url, first_seen, last_seen, seen_count)
                VALUES (?, ?, ?, ?, ?)
                """,
                batch,
            )
            inserted += conn.total_changes
        conn.commit()

        cur = conn.execute("SELECT COUNT(*) FROM dedup_seen_urls")
        after = cur.fetchone()[0]
        print(f"dedup_seen_urls after: {after}")
        print(f"  Inserted: {after - before}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
