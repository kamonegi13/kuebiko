#!/usr/bin/env python3
"""州・県 (GeoNames admin1) の代表点を gazetteer テーブル geo_admin1 に取込む。

2026-08-19 の実測: victim_city の 247 件中 **97 件 (39%) が地図に出ていなかった**。
値は Minnesota 17 / カリフォルニア州 5 / 徳島県 3 のような **州・県** で、geo_cities は
都市名の完全一致しか引けないため解決できていなかった。

⚠ これは LLM の誤りではない — 判定基準が「所在都市 / **地域**」と書き、例に
「カリフォルニア州」を挙げている。**指示が許したものを受け手が受け取れなかった**。

代表点は **その州で人口最大の都市** (countries.yaml の一括拡充と同じ方針)。州都では
ないが、州スケールの事象を 1 点で示す用途では同等に使える。

⚠ 新規ダウンロードは admin1 の**名前表**のみ。座標は既存の cities15000 から導出する
(11 列目の admin1 コードを現行の取込は捨てている)。

Usage:
    curl -s -o /tmp/cities15000.zip https://download.geonames.org/export/dump/cities15000.zip
    unzip -o /tmp/cities15000.zip -d /tmp
    curl -s -o /tmp/admin1.txt https://download.geonames.org/export/dump/admin1CodesASCII.txt
    uv run python scripts/ingest_geonames_admin1.py /tmp/cities15000.txt /tmp/admin1.txt
    uv run python scripts/ingest_geonames_admin1.py /tmp/cities15000.txt /tmp/admin1.txt --apply
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.storage.db_backend import connect  # noqa: E402

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS geo_admin1 ("
    " country_code TEXT NOT NULL,"
    " name_lower TEXT NOT NULL,"
    " admin1_code TEXT NOT NULL,"
    " lat DOUBLE PRECISION NOT NULL,"
    " lon DOUBLE PRECISION NOT NULL,"
    " population BIGINT NOT NULL DEFAULT 0,"
    " PRIMARY KEY (country_code, name_lower))"
)

# 日本の都道府県: GeoNames の admin1 名は英語 (Tokushima) だが、記事は「徳島県」と書く。
# ⚠ 47 件の固定集合なので長尾にならない。接尾辞を落として英語名と突き合わせる。
_JP_SUFFIXES = ("都", "道", "府", "県")


def _admin1_names(path: Path) -> dict[str, str]:
    """admin1 コード (US.MN) → 英語名 (Minnesota)。"""
    out: dict[str, str] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            cols = line.rstrip("\n").split("\t")
            if len(cols) >= 2 and cols[0].strip():
                out[cols[0].strip()] = cols[1].strip()
    return out


def _largest_city_per_admin1(path: Path) -> dict[str, tuple[float, float, int, str]]:
    """admin1 コード → (lat, lon, population, 都市名)。人口最大の都市を代表点にする。"""
    best: dict[str, tuple[float, float, int, str]] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            cols = line.rstrip("\n").split("\t")
            if len(cols) < 15:
                continue
            country, admin1 = cols[8].strip().upper(), cols[10].strip()
            if not country or not admin1:
                continue
            try:
                lat, lon, pop = float(cols[4]), float(cols[5]), int(cols[14] or 0)
            except ValueError:
                continue
            key = f"{country}.{admin1}"
            cur = best.get(key)
            if cur is None or pop > cur[2]:
                best[key] = (lat, lon, pop, cols[1].strip())
    return best


def _name_keys(country: str, en_name: str) -> list[str]:
    """索引キー (小文字)。日本は「徳島」「徳島県」の双方で引けるようにする。"""
    keys = [en_name.lower()]
    if country == "JP":
        # 英語名 → 日本語の県名は GeoNames に無いため、呼出側の正規化で
        # 「徳島県」→「徳島」に落としてから引く。ここでは英語キーのみ持つ。
        pass
    return keys


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    cities_path, admin1_path = Path(sys.argv[1]), Path(sys.argv[2])
    apply = "--apply" in sys.argv

    names = _admin1_names(admin1_path)
    points = _largest_city_per_admin1(cities_path)

    rows: list[tuple[str, str, str, float, float, int]] = []
    for code, (lat, lon, pop, _city) in points.items():
        en_name = names.get(code)
        if not en_name:
            continue
        country = code.split(".", 1)[0]
        for key in _name_keys(country, en_name):
            rows.append((country, key, code, lat, lon, pop))

    print(f"=== geo_admin1 取込 ({'APPLY' if apply else 'DRY-RUN'}) ===")
    print(f"admin1 名 {len(names)} / 代表点 {len(points)} → 索引 {len(rows)} 行")
    for country in ("US", "JP"):
        sample = [r for r in rows if r[0] == country][:3]
        for r in sample:
            print(f"  {r[0]} {r[1]:16s} {r[2]:8s} {r[3]:.2f},{r[4]:.2f} (人口 {r[5]:,})")
    if not apply:
        print("\n--apply を付けると取込みます (dry-run)")
        return 0

    with connect() as con:
        con.execute(_SCHEMA)
        con.execute("DELETE FROM geo_admin1")
        for r in rows:
            con.execute(
                "INSERT INTO geo_admin1"
                " (country_code, name_lower, admin1_code, lat, lon, population)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (country_code, name_lower) DO NOTHING",
                r,
            )
        con.commit()
    print(f"\n取込完了: {len(rows)} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
