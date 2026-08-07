"""PIR migration の behavior-preservation 検証 (A/B test)。

過去 N 日 (default 7 日) の posted articles を sample 抽出し:
  - A 系: legacy hardcoded prompt で triage
  - B 系: PIR-driven prompt で triage
両者の importance 判定を比較し、criteria 達成可否を report する。

verification criteria (CTI mission に基づく):
  - 全体 agreement rate: >= 90%
  - high → low の flip: 0 件 (critical signal を失わない)
  - medium → low の flip: <= 2%
  - JP feeds の medium+ 維持率: >= 95%
  - 重要 actor (Volt Typhoon, Lazarus 等) の high 維持率: >= 95%

Usage:
    uv run python scripts/verify_pir_migration.py            # sample 30 件で quick
    uv run python scripts/verify_pir_migration.py --n 300   # 本格 300 件
    uv run python scripts/verify_pir_migration.py --days 14 --n 100
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

from src.config_loader import load_app_config
from src.tools.article_model import Article
from src.tools.article_triage import ArticleTriage
from src.tools.llm_client import OllamaClient

JP_FEED_KEYWORDS = ["ScanNet", "Security NEXT", "セキュリティニュース", "ITmedia エンタープライズ", "@IT セキュリティ", "JPCERT"]
KEY_ACTORS = ["Volt Typhoon", "Salt Typhoon", "APT41", "Lazarus", "Kimsuky", "Sandworm", "APT28", "APT29"]


async def fetch_samples(db_path: Path, n: int, lookback_days: int) -> list[Article]:
    """過去 N 日の posted articles から sample 抽出。"""
    since = (datetime.now(UTC) - timedelta(days=lookback_days)).isoformat()
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT article_id, title, url, feed_title, summary, importance
              FROM articles
             WHERE status='posted' AND datetime(created_at) >= datetime(?)
             ORDER BY RANDOM()
             LIMIT ?
            """,
            (since, n),
        ).fetchall()
    samples: list[Article] = []
    from datetime import datetime as _dt
    now = _dt.now(UTC)
    for r in rows:
        samples.append(Article(
            id=r["article_id"],
            title=r["title"] or "",
            url=r["url"] or "",
            feed_title=r["feed_title"] or "",
            summary_html=r["summary"] or "",
            published=now,
            feed_url="",
        ))
    return samples


async def verify(n: int, days: int) -> int:
    """A/B test を実行し、結果を report。Returns: exit code (0=pass, 1=fail)。"""
    load_dotenv()
    cfg = load_app_config()
    db_path = Path("data/run_history.db")

    samples = await fetch_samples(db_path, n, days)
    print(f"Sampled {len(samples)} articles from past {days} days\n")

    # Both prompts share the same LLM client
    llm = OllamaClient(
        base_url=cfg.ollama_base_url,
        model=cfg.ollama_main_model,
    )
    triage = ArticleTriage(llm=llm, think=False)

    # Run A (legacy hardcoded) and B (PIR-driven) per article
    # — switch via env var PIR_DRIVEN_TRIAGE
    results: list[dict] = []
    for i, article in enumerate(samples, 1):
        # A: legacy
        os.environ["PIR_DRIVEN_TRIAGE"] = "0"
        from src.pir.integration import invalidate_cache
        invalidate_cache()
        a_decision = await triage.triage(article)

        # B: PIR-driven
        os.environ["PIR_DRIVEN_TRIAGE"] = "1"
        invalidate_cache()
        b_decision = await triage.triage(article)

        results.append({
            "article": article,
            "legacy": a_decision.importance,
            "pir_driven": b_decision.importance,
            "is_jp_feed": any(kw in (article.feed_title or "") for kw in JP_FEED_KEYWORDS),
            "is_key_actor": any(actor in (article.title or "") or actor in (article.summary_html or "")
                                for actor in KEY_ACTORS),
        })
        if i % 10 == 0:
            print(f"  {i}/{len(samples)} done...")

    # Reset env
    os.environ["PIR_DRIVEN_TRIAGE"] = "1"

    # Analyze
    agreements = sum(1 for r in results if r["legacy"] == r["pir_driven"])
    agreement_rate = agreements / len(results) if results else 0.0

    high_to_low = [r for r in results if r["legacy"] == "high" and r["pir_driven"] == "low"]
    medium_to_low = [r for r in results if r["legacy"] == "medium" and r["pir_driven"] == "low"]

    jp_results = [r for r in results if r["is_jp_feed"]]
    jp_medium_plus_legacy = [r for r in jp_results if r["legacy"] in ("high", "medium")]
    jp_kept_medium_plus = [r for r in jp_medium_plus_legacy if r["pir_driven"] in ("high", "medium")]
    jp_retention = len(jp_kept_medium_plus) / len(jp_medium_plus_legacy) if jp_medium_plus_legacy else 1.0

    actor_results = [r for r in results if r["is_key_actor"]]
    actor_high_legacy = [r for r in actor_results if r["legacy"] == "high"]
    actor_kept_high = [r for r in actor_high_legacy if r["pir_driven"] == "high"]
    actor_retention = len(actor_kept_high) / len(actor_high_legacy) if actor_high_legacy else 1.0

    # Report
    print("\n" + "=" * 70)
    print("PIR Migration A/B Verification Report")
    print("=" * 70)
    print()
    print(f"Sample size: {len(results)} articles, past {days} days")
    print()
    print(f"  Overall agreement rate:           {agreement_rate*100:.1f}% (target: >=90%)")
    print(f"  high → low flip (CRITICAL):       {len(high_to_low)} (target: 0)")
    print(f"  medium → low flip:                {len(medium_to_low)} ({len(medium_to_low)/len(results)*100:.1f}%, target: <=2%)")
    print(f"  JP feed medium+ retention:        {jp_retention*100:.1f}% ({len(jp_kept_medium_plus)}/{len(jp_medium_plus_legacy)}, target: >=95%)")
    print(f"  Key actor high retention:         {actor_retention*100:.1f}% ({len(actor_kept_high)}/{len(actor_high_legacy)}, target: >=95%)")
    print()

    # Flip 内訳 (全 9 patterns)
    from collections import Counter
    pattern_counts = Counter((r["legacy"], r["pir_driven"]) for r in results)
    print("  Decision flip breakdown (legacy → pir):")
    for (a, b), n in sorted(pattern_counts.items(), key=lambda x: -x[1]):
        marker = "✅" if a == b else "🔄"
        print(f"    {marker} {a:6s} → {b:6s}: {n:3d} ({n/len(results)*100:5.1f}%)")
    print()

    # Pass/fail
    failures: list[str] = []
    if agreement_rate < 0.90:
        failures.append(f"agreement {agreement_rate*100:.1f}% < 90%")
    if len(high_to_low) > 0:
        failures.append(f"high→low flip: {len(high_to_low)} > 0")
    if len(medium_to_low) / max(len(results), 1) > 0.02:
        failures.append(f"medium→low flip rate {len(medium_to_low)/len(results)*100:.1f}% > 2%")
    if jp_medium_plus_legacy and jp_retention < 0.95:
        failures.append(f"JP retention {jp_retention*100:.1f}% < 95%")
    if actor_high_legacy and actor_retention < 0.95:
        failures.append(f"Actor retention {actor_retention*100:.1f}% < 95%")

    if failures:
        print("❌ FAIL:")
        for f in failures:
            print(f"  - {f}")
        if high_to_low:
            print("\n  high→low flip 詳細:")
            for r in high_to_low[:5]:
                print(f"    - {r['article'].feed_title}: {r['article'].title[:60]}")
        return 1
    else:
        print("✅ PASS: behavior preservation OK")
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=30, help="sample 件数 (default 30、本格は 300)")
    parser.add_argument("--days", type=int, default=7, help="lookback 日数 (default 7)")
    args = parser.parse_args()
    sys.exit(asyncio.run(verify(args.n, args.days)))


if __name__ == "__main__":
    main()
