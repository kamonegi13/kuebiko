"""Phase F: V2 /discover endpoint で 108-feed self-consistency 検証。

V1 /auto_detect との比較対象。 同じ判定基準で結果集計。
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yaml

API_URL = "http://127.0.0.1:8001/api/v1/sources/discover"
FEEDS_YAML = Path("config/sources/feeds.yaml")
TIMEOUT = 120.0

THIRD_PARTY_HOSTS = {
    "feeds.feedburner.com",
    "feedburner.google.com",
    "rss.app",
    "feedmix.novaclic.com",
}


def _norm(url: str) -> str:
    u = urlparse(url)
    path = u.path.rstrip("/")
    return f"{u.scheme}://{u.netloc}{path}".lower()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=999)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--include-third-party", action="store_true")
    args = parser.parse_args()

    with FEEDS_YAML.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    feeds = [x for x in (cfg.get("feeds") or []) if x.get("enabled")]
    if not args.include_third_party:
        feeds = [f for f in feeds if urlparse(f["url"]).netloc not in THIRD_PARTY_HOSTS]
    random.seed(args.seed)
    sample = random.sample(feeds, min(args.n, len(feeds)))

    print(f"\n{'='*78}")
    print(f"V2 /discover endpoint で {len(sample)} feeds を verify")
    print(f"{'='*78}\n")

    stats = {"EXACT": 0, "EQUIV": 0, "ALT_RSS": 0, "SITEMAP_ONLY": 0, "MISSING": 0, "ERROR": 0}

    for feed in sample:
        name = feed["name"]
        feed_url = feed["url"]
        parsed = urlparse(feed_url)
        site_url = f"{parsed.scheme}://{parsed.netloc}/"
        feed_norm = _norm(feed_url)

        t_start = time.time()
        try:
            r = httpx.post(API_URL, json={"url": site_url, "max_preview_per_candidate": 3}, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            elapsed = time.time() - t_start
            stats["ERROR"] += 1
            print(f"  ✗ ERROR    {name:42s}  ({elapsed:.1f}s) {e}")
            continue
        elapsed = time.time() - t_start

        candidates = data.get("candidates", [])
        # transport ごとに分類
        rss_candidates = [c for c in candidates if c["transport"] in ("rss", "atom")]
        sm_candidates = [c for c in candidates if c["transport"] == "sitemap"]
        rss_urls = [c["fetch_url"] for c in rss_candidates]
        rss_norms = [_norm(u) for u in rss_urls]

        if feed_norm in rss_norms:
            stats["EXACT"] += 1
            verdict = "✓ EXACT  "
        elif any(n == feed_norm for n in rss_norms):
            stats["EQUIV"] += 1
            verdict = "~ EQUIV  "
        elif rss_candidates:
            stats["ALT_RSS"] += 1
            verdict = "→ ALT_RSS"
        elif sm_candidates:
            stats["SITEMAP_ONLY"] += 1
            verdict = "S SITEMAP"
        else:
            stats["MISSING"] += 1
            verdict = "✗ MISSING"

        print(f"  {verdict}  {name:42s}  ({elapsed:.1f}s)  rss={len(rss_candidates)} sm={len(sm_candidates)}")

    print(f"\n{'='*78}")
    print("SUMMARY (V2 /discover)")
    print(f"{'='*78}")
    total = sum(stats.values())
    for k, v in stats.items():
        if total > 0:
            print(f"  {k:13s} {v:3d}  ({v/total*100:.0f}%)")
    success = (stats["EXACT"] + stats["EQUIV"] + stats["ALT_RSS"]) / total if total else 0
    any_source = (success + stats["SITEMAP_ONLY"]/total) if total else 0
    print(f"\n  ANY RSS (実用): {success*100:.0f}%")
    print(f"  RSS or sitemap: {any_source*100:.0f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
