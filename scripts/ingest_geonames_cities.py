#!/usr/bin/env python3
"""3D-a: GeoNames cities15000 を gazetteer テーブル geo_cities に取込む (一度きり)。

被害都市 (victim_city) を都市レベル座標に解決するためのオフライン辞書。GeoNames
(public domain, CC-BY 4.0) の cities15000 (人口 1.5 万人以上、約 3.4 万都市) を使う。
name / asciiname / alternatenames を (country_code, name_lower) キーで索引し、同名衝突は
**人口最大の都市を採用** (例: JP+"osaka" → 大阪市)。alternatenames で日本語・現地語名も解決可。

エアギャップ: 外部 DL は **dev-time の一度きり generic 一括取得** (個別被害名は送らない)。
runtime は geo_cities への DB lookup のみ (egress なし)。

Usage:
    curl -s -o /tmp/cities15000.zip https://download.geonames.org/export/dump/cities15000.zip
    unzip -o /tmp/cities15000.zip -d /tmp
    uv run python scripts/ingest_geonames_cities.py /tmp/cities15000.txt          # dry-run
    uv run python scripts/ingest_geonames_cities.py /tmp/cities15000.txt --apply  # 取込
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.storage.db_backend import connect  # noqa: E402

# alternatenames の取込上限。日本語等の現地語名は末尾側に来る (例: 大阪 は alt 31 番目、
# 東京 は 43 番目) ため広めに取る。junk は短語/大文字コード除外 + 人口優先 dedup で抑制。
_MAX_ALT = 100
# 取込する最小都市人口 (cities15000 自体が >=15000 だが念のため)。
_MIN_POP = 0


def _name_keys(name: str, asciiname: str, alt: str) -> list[str]:
    """1 都市の索引キー候補 (小文字)。name / asciiname / alternatenames(上限)。"""
    keys: list[str] = []
    seen: set[str] = set()
    cands = [name, asciiname, *alt.split(",")[:_MAX_ALT]]
    for c in cands:
        k = c.strip().lower()
        if len(k) < 2:
            continue
        # 全大文字 ASCII の短いコード (空港コード等) は誤マッチの元なので除外
        if len(k) <= 4 and c.strip().isascii() and c.strip().isupper():
            continue
        if k not in seen:
            seen.add(k)
            keys.append(k)
    return keys


def _build_index(path: Path) -> dict[tuple[str, str], tuple[float, float, int]]:
    """(country_code, name_lower) → (lat, lon, population)。同キーは人口最大を採用。"""
    index: dict[tuple[str, str], tuple[float, float, int]] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            cols = line.rstrip("\n").split("\t")
            if len(cols) < 15:
                continue
            try:
                lat, lon = float(cols[4]), float(cols[5])
                pop = int(cols[14] or 0)
            except ValueError:
                continue
            cc = cols[8].strip().upper()
            if not cc or pop < _MIN_POP:
                continue
            for key in _name_keys(cols[1], cols[2], cols[3]):
                k = (cc, key)
                cur = index.get(k)
                if cur is None or pop > cur[2]:
                    index[k] = (lat, lon, pop)
    return index


_DDL = (
    "CREATE TABLE IF NOT EXISTS geo_cities ("
    "country_code TEXT NOT NULL, name_lower TEXT NOT NULL, "
    "lat DOUBLE PRECISION NOT NULL, lon DOUBLE PRECISION NOT NULL, "
    "population BIGINT NOT NULL DEFAULT 0, "
    "PRIMARY KEY (country_code, name_lower))"
)


def main(path: Path, apply: bool) -> int:
    index = _build_index(path)
    print(f"=== GeoNames cities → geo_cities: {len(index)} 件の (国, 名前) キー ===")
    sample = [("JP", "osaka"), ("DE", "munich"), ("JP", "大阪"), ("US", "new york")]
    for cc, nm in sample:
        v = index.get((cc, nm))
        print(f"  {cc}/{nm}: {v}")
    if not apply:
        print("\n[dry-run] 書き込みなし。--apply で geo_cities を作成/再投入する。")
        return 0

    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(_DDL)
        cur.execute("DELETE FROM geo_cities")
        rows = [(cc, nm, v[0], v[1], v[2]) for (cc, nm), v in index.items()]
        cur.executemany(
            "INSERT INTO geo_cities (country_code, name_lower, lat, lon, population) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        print(f"\n[applied] geo_cities に {len(rows)} 行を投入した。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: ingest_geonames_cities.py <cities15000.txt> [--apply]", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(Path(sys.argv[1]), apply="--apply" in sys.argv[1:]))
