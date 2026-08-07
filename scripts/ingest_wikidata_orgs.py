#!/usr/bin/env python3
"""3D-b: Wikidata から組織の本社座標 (P159→P625) を geo_orgs に取込む (一度きり)。

被害組織 (victim_org) を組織本社レベル (ORG_HQ tier) に解決するためのオフライン辞書。
**generic な type 別一括取得** (個別被害名は送らない → egress 実質無し、エアギャップ的に
問題なし)。Wikidata に本社座標がある **著名組織のみ** が対象 (無名企業の長い尾は別手段)。

en + ja ラベルを索引するので英語名・日本語名どちらでも解決可。名前衝突は first-wins。

Usage:
    uv run python scripts/ingest_wikidata_orgs.py            # dry-run (取得のみ)
    uv run python scripts/ingest_wikidata_orgs.py --apply    # geo_orgs に投入
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.storage.db_backend import connect  # noqa: E402

_ENDPOINT = "https://query.wikidata.org/sparql"
_UA = "kuebiko-geo/1.0 (offline gazetteer build; research)"

# CTI 被害として頻出する組織 type (QID)。subclass walk は遅いので P31 直指定。
_TYPES: list[tuple[str, str]] = [
    ("Q891723", "public company"),
    ("Q22687", "bank"),
    ("Q3918", "university"),
    ("Q2085381", "publisher"),
    ("Q4830453", "business enterprise"),
    ("Q163740", "nonprofit organization"),
    ("Q327333", "government agency"),
    ("Q16917", "hospital"),
    ("Q1254933", "astronomical observatory"),
    ("Q11691", "stock exchange"),
]

# 全 type を 1 クエリ (VALUES) で取得 → **リクエスト1回** で WDQS の 1req/分 制限に当たらない。
def _build_query() -> str:
    values = " ".join(f"wd:{qid}" for qid, _ in _TYPES)
    return f"""
SELECT ?org ?label ?lat ?lon WHERE {{
  VALUES ?type {{ {values} }}
  ?org wdt:P31 ?type ; wdt:P159 ?hq .
  ?hq wdt:P625 ?coord .
  ?org rdfs:label ?label . FILTER(lang(?label) IN ('en','ja'))
  BIND(geof:latitude(?coord) AS ?lat)
  BIND(geof:longitude(?coord) AS ?lon)
}}
"""


def _run_query(query: str, *, retries: int = 3) -> list[dict[str, object]]:
    """WDQS に 1 リクエスト。429 (1req/分制限) は 65s 待って retry。"""
    url = _ENDPOINT + "?" + urllib.parse.urlencode({"query": query, "format": "json"})
    headers = {"User-Agent": _UA, "Accept": "application/sparql-results+json"}
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=headers)  # noqa: S310
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:  # noqa: S310
                data = json.loads(resp.read().decode("utf-8"))
            return list(data.get("results", {}).get("bindings", []))
        except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
            if e.code == 429 and attempt < retries - 1:
                msg = f"  [429] rate-limited, 65s wait + retry ({attempt + 1}/{retries})"
                print(msg, file=sys.stderr)
                time.sleep(65)
                continue
            raise
    return []


def _collect() -> dict[str, tuple[float, float]]:
    """name_lower → (lat, lon)。1 クエリ取得、同名は first-wins。"""
    rows = _run_query(_build_query())
    print(f"  WDQS から {len(rows)} 行 (org × en/ja label)")
    out: dict[str, tuple[float, float]] = {}
    for r in rows:
        try:
            name = str(r["label"]["value"]).strip()
            lat = float(r["lat"]["value"])
            lon = float(r["lon"]["value"])
        except (KeyError, ValueError, TypeError):
            continue
        key = name.lower()
        if len(key) < 2 or key in out:
            continue
        out[key] = (lat, lon)
    return out


_DDL = (
    "CREATE TABLE IF NOT EXISTS geo_orgs ("
    "name_lower TEXT PRIMARY KEY, lat DOUBLE PRECISION NOT NULL, lon DOUBLE PRECISION NOT NULL)"
)


def main(apply: bool) -> int:
    print("=== Wikidata 組織本社 → geo_orgs ===")
    index = _collect()
    print(f"\n合計 {len(index)} 組織")
    for probe in ("morgan stanley", "sony", "トヨタ自動車", "toyota"):
        print(f"  {probe}: {index.get(probe)}")
    if not apply:
        print("\n[dry-run] 書き込みなし。--apply で geo_orgs を作成/再投入する。")
        return 0
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(_DDL)
        cur.execute("DELETE FROM geo_orgs")
        cur.executemany(
            "INSERT INTO geo_orgs (name_lower, lat, lon) VALUES (?, ?, ?)",
            [(k, v[0], v[1]) for k, v in index.items()],
        )
        conn.commit()
        print(f"\n[applied] geo_orgs に {len(index)} 行を投入した。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(apply="--apply" in sys.argv[1:]))
