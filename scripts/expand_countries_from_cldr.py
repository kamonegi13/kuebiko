#!/usr/bin/env python3
"""``countries.yaml`` と ``geocoder._COUNTRY_CENTROIDS`` を全 ISO 国へ拡張する。

2026-08-19 の実測: 辞書は 117 か国しか収録しておらず、Liechtenstein / Albania /
Botswana / Cyprus / Mauritius / Paraguay / Uruguay / Ghana などが解決できず、
**被害国が地図と国別 KPI から静かに落ちていた** (60 日で約 30 件)。LLM は ISO コード
(``AL``) と英語名 (``Albania``) の両方を返すため、どちらでも引けるようにする。

出典を手書きにしない:
  - 国名 (日本語 / 英語) = CLDR (babel、既存依存)
  - 座標 = DB の ``geo_cities`` (GeoNames、339,235 都市 / 244 か国)

⚠ **座標は「首都」ではなく「最大人口都市」**。gazetteer に首都フラグが無いため。
   国バブルの代表点としては実用上問題ないが、既存 117 件 (首都点) と由来が違うので
   geocoder 側のコメントに明記する。

⚠ **既存エントリは 1 文字も触らない**。手で育てた alias (``日系`` / ``JPN`` 等) と
   セクション見出しコメントを機械生成で潰さないため、**parse-and-dump ではなく
   テキスト追記**で書く。

Usage::

    uv run python scripts/expand_countries_from_cldr.py          # 差分表示
    uv run python scripts/expand_countries_from_cldr.py --apply  # 書き込み
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml
from babel import Locale

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO if (_REPO / "src").is_dir() else Path("/app")))

YAML_PATH = Path("config/cti/countries.yaml")
GEOCODER_PATH = Path("src/cti/geocoder.py")

# 国でないコード。⚠ EU を国として足すと 2026-08-01 に作った scope 経路
# (victim_country_scope='regional') が発火しなくなる。
NON_COUNTRY = frozenset({"EU", "EZ", "UN", "QO", "XA", "XB", "ZZ"})

_CENTROID_SQL = """
    SELECT country_code, lat, lon FROM (
        SELECT country_code, lat, lon,
               ROW_NUMBER() OVER (PARTITION BY country_code ORDER BY population DESC) AS rn
        FROM geo_cities
    ) t WHERE rn = 1
"""


def _representative_points() -> dict[str, tuple[float, float]]:
    """国ごとの代表点 (最大人口都市) を gazetteer から引く。"""
    from src.storage.run_history import RunHistoryRepository

    with RunHistoryRepository()._connect() as conn:  # noqa: SLF001 — 一回限りの保守
        rows = conn.execute(_CENTROID_SQL).fetchall()
    out: dict[str, tuple[float, float]] = {}
    for row in rows:
        values = list(dict(row).values()) if not isinstance(row, (list, tuple)) else list(row)
        out[str(values[0]).upper()] = (round(float(values[1]), 2), round(float(values[2]), 2))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="実際に書き込む")
    args = ap.parse_args()

    existing = set(yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))["canonical"])
    points = _representative_points()
    ja, en = Locale("ja"), Locale("en")

    additions: list[tuple[str, str, str, tuple[float, float]]] = []
    no_point: list[str] = []
    for code in sorted(ja.territories):
        if len(code) != 2 or not code.isalpha() or code in NON_COUNTRY or code in existing:
            continue
        point = points.get(code)
        if point is None:
            no_point.append(code)  # 座標が無い国は足さない (drift-guard を壊さない)
            continue
        additions.append((code, ja.territories[code], en.territories.get(code, code), point))

    total = len(existing) + len(additions)
    print(f"既存 {len(existing)} 件 → 追加 {len(additions)} 件 → 計 {total} 件")
    if no_point:
        print(f"⚠ 座標が gazetteer に無く見送り {len(no_point)} 件: {', '.join(no_point)}")
    for code, ja_name, _en, point in additions[:5]:
        print(f"  + {code}: {ja_name} {point}")
    if len(additions) > 5:
        print(f"  … 他 {len(additions) - 5} 件")

    if not args.apply:
        print("\n--apply を付けると書き込みます (dry-run)")
        return 0

    yaml_block = [
        "",
        "  # ===== CLDR 由来の一括拡充 (2026-08-19) =====",
        "  # 収録 117 か国では Albania / Botswana / Cyprus 等が解決できず、被害国が地図と",
        "  # 国別 KPI から静かに落ちていた (60 日で約 30 件)。国名は CLDR、座標は gazetteer。",
        "  # alias は LLM が返しうる表記 = 日本語名 / 英語名 / ISO2。",
    ]
    for code, ja_name, en_name, _p in additions:
        aliases = ", ".join(f'"{a}"' for a in dict.fromkeys([ja_name, en_name, code]))
        yaml_block += [f"  {code}:", f"    display: {ja_name}", f"    aliases: [{aliases}]"]
    YAML_PATH.write_text(
        YAML_PATH.read_text(encoding="utf-8").rstrip("\n") + "\n" + "\n".join(yaml_block) + "\n",
        encoding="utf-8",
    )

    src = GEOCODER_PATH.read_text(encoding="utf-8")
    marker = "_COUNTRY_CENTROIDS: dict[str, tuple[float, float]] = {"
    end = src.index("\n}", src.index(marker))
    centroid_block = [
        "",
        "    # ===== 一括拡充 (2026-08-19) =====",
        "    # ⚠ 上の 117 件は首都点だが、ここは **最大人口都市** (gazetteer に首都フラグが",
        "    # 無いため)。国バブルの代表点としては同等に使える。",
    ]
    centroid_block += [
        f'    "{code}": ({p[0]}, {p[1]}),  # {ja_name}' for code, ja_name, _e, p in additions
    ]
    GEOCODER_PATH.write_text(src[:end] + "\n" + "\n".join(centroid_block) + src[end:], "utf-8")
    print(f"\n書き込み: {YAML_PATH} / {GEOCODER_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
