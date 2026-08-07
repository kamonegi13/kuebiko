"""破損 SQLite DB から URL / 重要データを raw page parsing で抽出。

SQLite DB header が壊れて connect() できないケースで使う最終手段。
file の binary を直接 scan して URL pattern と article 候補を抽出する。

Usage (container 内で実行):
    docker exec kuebiko /app/.venv/bin/python3 /app/scripts/recover_db.py
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

DB_PATH = Path("/app/data/run_history.db.corrupt_backup_1779744909")
OUTPUT_DIR = Path("/app/data/recovery")

# SQLite page format markers (1st byte of page header):
# 0x0D = table leaf, 0x05 = table interior, 0x0A = index leaf, 0x02 = index interior
PAGE_TYPES = {0x02: "idx_int", 0x05: "tbl_int", 0x0A: "idx_leaf", 0x0D: "tbl_leaf"}

# よく使われる page size 候補
PAGE_SIZES = (4096, 8192, 16384, 32768)

# URL like pattern (http(s)://)
URL_RE = re.compile(rb"https?://[a-zA-Z0-9./_\-?&=#%+~:!,;@$()*'\[\]]+")

# CVE pattern
CVE_RE = re.compile(rb"CVE-\d{4}-\d{4,7}")


def scan_urls(data: bytes, limit: int = 1_000_000) -> Counter[str]:
    """binary から URL 文字列を全部抽出してカウント。"""
    counts: Counter[str] = Counter()
    found = 0
    for m in URL_RE.finditer(data):
        if found >= limit:
            break
        try:
            url = m.group().decode("utf-8", errors="ignore")
            # ノイズ除去: 末尾の余計な記号
            url = url.rstrip("'\")>,.;:?#")
            if 10 <= len(url) <= 500:
                counts[url] += 1
                found += 1
        except UnicodeDecodeError:
            continue
    return counts


def scan_cves(data: bytes) -> Counter[str]:
    """binary から CVE ID を抽出。"""
    counts: Counter[str] = Counter()
    for m in CVE_RE.finditer(data):
        cve = m.group().decode("utf-8", errors="ignore")
        counts[cve] += 1
    return counts


def detect_page_layout(data: bytes) -> tuple[int, dict[str, int]]:
    """page size を推定し、page type 分布を返す。"""
    best_size = 4096
    best_score = 0
    for ps in PAGE_SIZES:
        # 全 page の先頭 byte が SQLite page type か検査
        type_counts: dict[str, int] = {}
        valid_pages = 0
        total_pages = len(data) // ps
        if total_pages == 0:
            continue
        for i in range(total_pages):
            offset = i * ps
            if offset >= len(data):
                break
            ptype = data[offset] if offset < len(data) else 0
            if ptype in PAGE_TYPES:
                name = PAGE_TYPES[ptype]
                type_counts[name] = type_counts.get(name, 0) + 1
                valid_pages += 1
        score = valid_pages / max(total_pages, 1)
        if score > best_score:
            best_score = score
            best_size = ps
    # best_size での詳細分布を返す
    type_counts: dict[str, int] = {}
    total_pages = len(data) // best_size
    for i in range(total_pages):
        offset = i * best_size
        ptype = data[offset] if offset < len(data) else 0
        if ptype in PAGE_TYPES:
            name = PAGE_TYPES[ptype]
            type_counts[name] = type_counts.get(name, 0) + 1
    return best_size, type_counts


def main() -> int:
    if not DB_PATH.exists():
        print(f"❌ Backup file not found: {DB_PATH}")
        return 1

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"=== Reading {DB_PATH} ({DB_PATH.stat().st_size:,} bytes) ===")
    data = DB_PATH.read_bytes()

    print("\n=== Page layout detection ===")
    page_size, page_types = detect_page_layout(data)
    print(f"  Estimated page size: {page_size}")
    print(f"  Page type distribution: {page_types}")
    total_pages = len(data) // page_size
    valid_pages = sum(page_types.values())
    print(f"  Valid pages: {valid_pages} / {total_pages} ({valid_pages * 100 // max(total_pages, 1)}%)")

    print("\n=== URL extraction ===")
    url_counts = scan_urls(data)
    print(f"  Unique URLs found: {len(url_counts)}")
    print(f"  Total URL occurrences: {sum(url_counts.values())}")

    # 上位 URL を sample 表示
    print("\n  Sample (top 10 by frequency):")
    for url, count in url_counts.most_common(10):
        print(f"    {count:5d}× {url[:100]}")

    # 全 URL を file に書き出し
    urls_path = OUTPUT_DIR / "recovered_urls.txt"
    with urls_path.open("w", encoding="utf-8") as f:
        for url, count in url_counts.most_common():
            f.write(f"{count}\t{url}\n")
    print(f"\n  ✓ All URLs saved to {urls_path}")

    print("\n=== CVE extraction ===")
    cve_counts = scan_cves(data)
    print(f"  Unique CVE IDs found: {len(cve_counts)}")
    cves_path = OUTPUT_DIR / "recovered_cves.txt"
    with cves_path.open("w", encoding="utf-8") as f:
        for cve, count in cve_counts.most_common():
            f.write(f"{count}\t{cve}\n")
    print(f"  ✓ All CVEs saved to {cves_path}")

    print("\n=== Recovery summary ===")
    print(f"  URLs recovered: {len(url_counts)}")
    print(f"  CVEs recovered: {len(cve_counts)}")
    print(f"  Output dir: {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
