"""既存 feeds.yaml 購読 site に対して auto_detect が正しい RSS を発見できるか self-consistency 検証。

各 feed の URL から site top を導出 → /api/v1/sources/auto_detect に投入 →
candidates に元の feed URL (または同等の RSS) が含まれているか確認。

判定基準:
  ✓ EXACT   = 元 feed URL と完全一致 candidate あり
  ~ EQUIV   = 同等の RSS は発見 (URL の trailing slash 違いや別 alias)
  → ALT_RSS = 別の RSS が発見 (元 URL は無いが site に RSS あり)
  ✗ MISSING = RSS 候補なし (sitemap or scraper のみ)

Note: feedburner や hatena 等の 第三者 hosted feed は site top probe では
      見つからないのが正常。 そのケースは "EXPECTED_MISS" として除外集計。
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

API_URL = "http://127.0.0.1:8001/api/v1/sources/auto_detect"
FEEDS_YAML = Path("config/sources/feeds.yaml")
TIMEOUT = 120.0  # 一部 site (Ransomware.live / iHLS 等) は probe が時間掛かる

# 第三者 hosted feed: site top probe で見つかるはずがない (除外集計)
THIRD_PARTY_HOSTS = {
    "feeds.feedburner.com",
    "feedburner.google.com",
    "rss.app",
    "feedmix.novaclic.com",
}


def _norm(url: str) -> str:
    """trailing slash / fragment / query を丸めて比較しやすく。"""
    u = urlparse(url)
    path = u.path.rstrip("/")
    return f"{u.scheme}://{u.netloc}{path}".lower()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=15, help="サンプル feed 数")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--include-third-party", action="store_true",
                        help="feedburner 等の hosted feed も対象に含める")
    args = parser.parse_args()

    with FEEDS_YAML.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    feeds = [x for x in (cfg.get("feeds") or []) if x.get("enabled")]
    print(f"loaded {len(feeds)} enabled feeds from {FEEDS_YAML}", file=sys.stderr)

    # 第三者 hosted を除外
    if not args.include_third_party:
        feeds = [f for f in feeds if urlparse(f["url"]).netloc not in THIRD_PARTY_HOSTS]
        print(f"after third-party exclusion: {len(feeds)}", file=sys.stderr)

    random.seed(args.seed)
    sample = random.sample(feeds, min(args.n, len(feeds)))

    print(f"\n{'='*78}")
    print(f"sampling {len(sample)} feeds for self-consistency test")
    print(f"{'='*78}\n")

    stats = {"EXACT": 0, "EQUIV": 0, "ALT_RSS": 0, "SITEMAP_ONLY": 0, "MISSING": 0, "ERROR": 0}
    failures: list[dict] = []

    for i, feed in enumerate(sample, 1):
        name = feed["name"]
        feed_url = feed["url"]
        parsed = urlparse(feed_url)
        site_url = f"{parsed.scheme}://{parsed.netloc}/"
        feed_norm = _norm(feed_url)

        t_start = time.time()
        try:
            r = httpx.post(
                API_URL, json={"url": site_url}, timeout=TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            elapsed = time.time() - t_start
            stats["ERROR"] += 1
            print(f"  ✗ ERROR    {name:42s}  ({elapsed:.1f}s) {e}")
            failures.append({"name": name, "feed_url": feed_url, "site_url": site_url, "error": str(e)})
            continue
        elapsed = time.time() - t_start

        rss_candidates = data.get("rss", {}).get("candidates", [])
        sm_candidates = data.get("sitemap", {}).get("candidates", [])
        rss_urls = [c["url"] for c in rss_candidates]
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
        if verdict in ("→ ALT_RSS", "✗ MISSING", "S SITEMAP"):
            print(f"      expected: {feed_url}")
            if rss_candidates:
                for c in rss_candidates[:3]:
                    print(f"      found rss: {c['url']}  (title={c.get('title')})")
            elif sm_candidates:
                for c in sm_candidates[:1]:
                    print(f"      found sm:  {c['url']}  ({c.get('url_count')} URLs)")
            failures.append({
                "name": name,
                "expected": feed_url,
                "found_rss": rss_urls,
                "found_sitemap": [c["url"] for c in sm_candidates],
                "verdict": verdict.strip(),
            })

    print(f"\n{'='*78}")
    print("SUMMARY")
    print(f"{'='*78}")
    total = sum(stats.values())
    for k, v in stats.items():
        if total > 0:
            print(f"  {k:13s} {v:3d}  ({v/total*100:.0f}%)")

    success_rate = (stats["EXACT"] + stats["EQUIV"]) / total if total else 0
    print(f"\n  exact + equiv (期待結果): {success_rate*100:.0f}%")
    if stats["MISSING"]:
        print(f"  ⚠ MISSING ({stats['MISSING']} 件): 既登録 site なのに auto_detect 候補なし")

    return 0


if __name__ == "__main__":
    sys.exit(main())
