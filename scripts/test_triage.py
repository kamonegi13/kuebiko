#!/usr/bin/env python3
"""ArticleTriage の判定品質を様々なサンプル記事で検証する (Phase 3.1)。"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.tools.article_model import Article  # noqa: E402
from src.tools.article_triage import ArticleTriage  # noqa: E402
from src.tools.llm_client import OllamaClient  # noqa: E402

# サンプル記事 — 13 種類の判定基準と low/medium 例を網羅
SAMPLES: list[tuple[str, str, str, str]] = [
    # (expected_importance, criterion#, title, summary)
    (
        "high",
        "1 中国APT",
        "Volt Typhoon Targets US Power Grid in Sustained Pre-positioning Campaign",
        "<p>CISA and NSA issued a joint advisory on Volt Typhoon's continued pre-"
        "positioning activity in US critical infrastructure. The Chinese state-sponsored "
        "actor has been observed leveraging living-off-the-land techniques.</p>",
    ),
    (
        "high",
        "2 北朝鮮APT・日本対象",
        "Lazarus Group Launches Targeted Phishing Against Japanese Defense Contractor",
        "<p>North Korean Lazarus Group has been observed sending spear-phishing emails to "
        "employees at a major Japanese defense contractor. The campaign uses fake job "
        "offers via LinkedIn.</p>",
    ),
    (
        "high",
        "3 イラン系・日本対象",
        "Iranian APT34 Targets Japanese Energy Company",
        "<p>APT34 (OilRig) has been observed targeting a Japanese energy company with "
        "Password Filter DLL attacks.</p>",
    ),
    (
        "high",
        "4 重要インフラOT",
        "Schneider Electric PLC Critical Vulnerability Actively Exploited in Water Utility",
        "<p>A critical vulnerability in Schneider Electric Modicon PLCs is being actively "
        "exploited against water utilities in Europe. CVE-2026-99999 allows unauthenticated "
        "remote code execution.</p>",
    ),
    (
        "high",
        "5 政府レベルランサム",
        "Ransomware Cripples 30 Hospitals Across UK, NHS Declares National Emergency",
        "<p>A massive ransomware attack has crippled 30 NHS hospitals across the UK, "
        "forcing the NHS to declare a national emergency. Patient care has been "
        "severely impacted.</p>",
    ),
    (
        "high",
        "6 APT関連企業内部リーク",
        "Massive Leak Exposes Chinese Cybersecurity Contractor i-Soon's APT Operations",
        "<p>A massive leak from Chinese cybersecurity contractor i-Soon (上海安洵) has "
        "exposed internal documents revealing MSS contracts, target lists, and APT41 "
        "tooling.</p>",
    ),
    (
        "high",
        "7 広域影響サプライチェーン",
        "Critical Vulnerability in OpenSSL Affects Millions of Servers Worldwide",
        "<p>A critical vulnerability discovered in OpenSSL 3.x affects millions of servers "
        "worldwide. Major cloud providers and CDNs are scrambling to patch.</p>",
    ),
    (
        "high",
        "9 AI/宇宙融合",
        "Russian Sandworm Tests AI-Powered Malware in Ukraine Conflict",
        "<p>Sandworm (APT44) has been observed deploying AI-powered malware against "
        "Ukrainian satellite ground stations, integrating cyber operations with "
        "electronic warfare.</p>",
    ),
    (
        "high",
        "10 帰属公表",
        "Five Eyes Alliance Formally Attributes Salt Typhoon Campaign to Chinese MSS",
        "<p>The Five Eyes alliance has formally attributed the Salt Typhoon telecom "
        "espionage campaign to China's MSS, naming specific units and individuals.</p>",
    ),
    (
        "high",
        "11 緊急アラート",
        "CISA Emergency Directive 25-99: Immediate Patching Required for Cisco IOS Critical CVE",
        "<p>CISA has issued an Emergency Directive requiring all federal agencies to "
        "patch CVE-2026-12345 in Cisco IOS within 24 hours due to active exploitation.</p>",
    ),
    (
        "high",
        "12 国家戦略",
        "China's New Cyber Doctrine: Integrated Network-Electronic Warfare in Taiwan Scenario",
        "<p>A think tank report analyzes China's evolving cyber doctrine, integrating "
        "network and electronic warfare for a potential Taiwan contingency.</p>",
    ),
    (
        "high",
        "13 影響力工作",
        "Russian GRU Unit 54777 Runs Disinformation Campaign Targeting US Election",
        "<p>Russian GRU influence operations (Unit 54777) have been observed running a "
        "disinformation campaign targeting the upcoming US election via fake social "
        "media accounts.</p>",
    ),
    (
        "medium",
        "M-1 既知APT継続",
        "APT29 Continues Long-Running Espionage Operation Against Western Diplomats",
        "<p>APT29 (Cozy Bear) continues its long-running espionage operation against "
        "Western diplomats, using the same TTPs observed for years.</p>",
    ),
    (
        "medium",
        "M-2 限定影響セキュリティ侵害",
        "Small Cybersecurity Vendor Discloses Customer Data Breach (5,000 Affected)",
        "<p>A small regional cybersecurity vendor has disclosed a data breach affecting "
        "5,000 customers. No APT involvement suspected.</p>",
    ),
    (
        "low",
        "L-1 単発金銭犯罪",
        "Local Restaurant Chain Hit by Ransomware, Operations Resume Within Day",
        "<p>A local restaurant chain in Germany was hit by ransomware. Operations resumed "
        "within a day after backup restoration.</p>",
    ),
    (
        "low",
        "L-2 製品マーケ",
        "Vendor Announces New AI-Powered SOC Platform at RSA Conference",
        "<p>Cybersecurity vendor X announces a new AI-powered SOC platform at RSA "
        "Conference 2026. Available Q3.</p>",
    ),
    (
        "low",
        "L-3 一般Tips",
        "10 Tips for Better Password Management in 2026",
        "<p>Here are 10 best practices for better password management this year, "
        "including using a password manager and enabling MFA everywhere.</p>",
    ),
]


def _make_article(title: str, summary: str) -> Article:
    return Article(
        id=f"test:{hash(title)}",
        title=title,
        url="https://example.com/x",
        summary_html=summary,
        author=None,
        published=datetime.now(UTC),
        feed_title="Test Feed",
        feed_url="https://example.com/feed",
    )


async def main() -> None:
    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    model = os.getenv("OLLAMA_EXTRACT_MODEL", "gemma4:26b")
    client = OllamaClient(base_url=base_url, model=model, timeout_seconds=300)
    triage = ArticleTriage(client)

    print(f"Model: {model}")
    print(f"Samples: {len(SAMPLES)}\n")
    print(f"{'expected':<8} {'actual':<8} {'criterion':<28} {'reason'}")
    print("-" * 100)
    correct = 0
    total_time = 0.0
    for expected, criterion, title, summary in SAMPLES:
        article = _make_article(title, summary)
        start = time.monotonic()
        d = await triage.triage(article)
        elapsed = time.monotonic() - start
        total_time += elapsed
        ok = d.importance == expected
        if ok:
            correct += 1
        marker = "✓" if ok else "✗"
        print(
            f"{expected:<8} {d.importance:<8} {criterion:<28} ({elapsed:.1f}s) {marker} {d.reason}"
        )
    print("-" * 100)
    print(
        f"Correct: {correct}/{len(SAMPLES)} ({100 * correct / len(SAMPLES):.0f}%) "
        f"avg {total_time / len(SAMPLES):.1f}s/article",
    )


if __name__ == "__main__":
    asyncio.run(main())
