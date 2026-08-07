#!/usr/bin/env python3
"""意味的重複排除タスクで Embedding モデルの判別精度と速度を比較する (Phase 3b)。

検証対象: 同じ事件 / 続報 / 異なる事件 / 無関係 の 4 種類のペアを各モデルで
embedding し、コサイン類似度を計測。望ましい挙動:

    - 同じ記事 (再配信)         → 高い類似度 (0.95+)
    - 続報・新情報追加            → 中〜高 (0.80〜0.93)、★ 重要: 過度に高くない
    - 同じアクター・別事件       → 中 (0.65〜0.80)
    - 全く別のトピック           → 低 (〜0.50)

候補モデル (Ollama 公式ライブラリで pull 可能、CLAUDE.md §4 準拠、中華系除外):
    - snowflake-arctic-embed2  (Snowflake, US) - 多言語 SOTA、推奨
    - nomic-embed-text-v2-moe  (Nomic, US) - 多言語、MoE
    - granite-embedding        (IBM, US) - 軽量多言語 (12 言語)
    - nomic-embed-text         (Nomic, US) - 英語 SOTA、軽量
    - mxbai-embed-large        (Mixedbread, DE) - 英語高品質

注: ``intfloat/multilingual-e5-*`` は Ollama 公式ライブラリ外のため割愛
(Hugging Face 経由で手動 import すれば利用可能だが手間)。

事前に各モデルを ``ollama pull`` してから実行する。

Usage:
    uv run python scripts/compare_embedding_models.py [--models a b c]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.tools.embedding_client import OllamaEmbeddingClient  # noqa: E402

# (label, text_a, text_b, expected_band)
# expected_band: 'duplicate' (高), 'followup' (中-高), 'related' (中), 'unrelated' (低)
PAIRS: list[tuple[str, str, str, str]] = [
    (
        "1 完全再配信 (同じ記事を別ソースが転載)",
        "Lazarus Group targets banking systems with new malware. "
        "North Korean state-sponsored Lazarus Group has launched a new campaign "
        "targeting banking systems globally. The malware uses LinkedIn fake job "
        "offers as initial vector.",
        "Lazarus Group targets banking systems with new malware. "
        "North Korean state-sponsored Lazarus Group has launched a new campaign "
        "targeting banking systems globally. The malware uses LinkedIn fake job "
        "offers as initial vector.",
        "duplicate",
    ),
    (
        "2 続報 (同じ事件 + 新情報追加)",
        "Lazarus targets banking systems. "
        "North Korean Lazarus Group hits banking infrastructure with new malware. "
        "Initial reports of attack via LinkedIn fake job offers.",
        "Lazarus banking attack: $50M stolen, FBI attribution confirmed. "
        "Following the initial report of Lazarus targeting banks via LinkedIn, "
        "FBI has now formally attributed the attack to North Korean state actors. "
        "Total losses estimated at $50 million across 12 institutions. "
        "New IOCs released by CISA include specific malware hashes and "
        "C2 infrastructure addresses.",
        "followup",
    ),
    (
        "3 同事件・言語違い (英→日)",
        "Volt Typhoon targets US power grid in pre-positioning campaign. "
        "Chinese state-sponsored APT continues sustained pre-positioning activity "
        "in US critical infrastructure. CISA and NSA issue joint advisory.",
        "中国系 APT Volt Typhoon、米電力網への事前配置を継続。"
        "国家支援型 APT である Volt Typhoon が米国の重要インフラへの事前配置活動を"
        "継続している。CISA と NSA が合同アドバイザリを発表。",
        "duplicate",
    ),
    (
        "4 同アクター・別事件",
        "Lazarus targets banking via LinkedIn fake jobs. "
        "Lazarus Group launches financial cyber operation through fake recruiter accounts.",
        "Lazarus deploys new ransomware against South Korean defense contractor. "
        "Different campaign by Lazarus Group, this time using ransomware against "
        "defense industrial base in South Korea.",
        "related",
    ),
    (
        "5 同テーマ・別アクター",
        "Lazarus Group conducts cryptocurrency theft from DeFi exchanges.",
        "APT41 conducts cryptocurrency theft from DeFi exchanges.",
        "related",
    ),
    (
        "6 無関係",
        "Lazarus Group targets banking systems with new malware.",
        "Apple announces new iPhone 17 Pro with improved camera.",
        "unrelated",
    ),
    (
        "7 同事件・短報 vs 詳報",
        "Volt Typhoon detected in US infrastructure.",
        "Volt Typhoon Chinese APT campaign detailed analysis. "
        "Comprehensive report on Volt Typhoon's tactics, techniques and procedures "
        "for sustained pre-positioning in US critical infrastructure including "
        "power, water, communications. Includes full IOC list, MITRE ATT&CK mapping, "
        "and detailed remediation guidance from CISA, NSA, and partner agencies.",
        "followup",
    ),
]


@dataclass
class Result:
    model: str
    duration_avg_s: float
    similarities: list[tuple[str, str, float]]  # (label, expected, sim)
    error: str | None = None


def _expected_band(expected: str) -> tuple[float, float]:
    """期待される類似度の上下限。"""
    return {
        "duplicate": (0.92, 1.01),  # 0.92+ で良好
        "followup": (0.65, 0.92),  # 0.65〜0.92 が理想 (高すぎても低すぎても困る)
        "related": (0.45, 0.80),
        "unrelated": (-1.0, 0.55),
    }[expected]


async def run_one(model: str, base_url: str) -> Result:
    print(f"\n=== {model} ===", flush=True)
    client = OllamaEmbeddingClient(base_url=base_url, model=model, timeout_seconds=300)
    durations: list[float] = []
    sims: list[tuple[str, str, float]] = []
    try:
        for label, a, b, expected in PAIRS:
            start = time.monotonic()
            r_a = await client.embed(a)
            r_b = await client.embed(b)
            durations.append(time.monotonic() - start)
            v_a = np.asarray(r_a.vector, dtype=np.float32)
            v_b = np.asarray(r_b.vector, dtype=np.float32)
            sim = float(np.dot(v_a, v_b) / (np.linalg.norm(v_a) * np.linalg.norm(v_b)))
            sims.append((label, expected, sim))
    except Exception as e:  # noqa: BLE001
        return Result(
            model=model,
            duration_avg_s=sum(durations) / max(1, len(durations)),
            similarities=sims,
            error=str(e),
        )
    return Result(
        model=model,
        duration_avg_s=sum(durations) / max(1, len(durations)),
        similarities=sims,
        error=None,
    )


def print_result(r: Result) -> None:
    if r.error:
        print(f"  ERROR: {r.error}")
        return
    print(f"  avg duration: {r.duration_avg_s:.2f}s/pair")
    print(f"  {'expected':<12} {'sim':<8} {'verdict':<8} label")
    print("  " + "-" * 90)
    correct = 0
    for label, expected, sim in r.similarities:
        lo, hi = _expected_band(expected)
        in_band = lo <= sim < hi
        verdict = "✓" if in_band else "✗"
        if in_band:
            correct += 1
        print(f"  {expected:<12} {sim:.3f}    {verdict:<8} {label}")
    print("  " + "-" * 90)
    print(
        f"  in-band: {correct}/{len(r.similarities)} ({100 * correct / len(r.similarities):.0f}%)",
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base-url",
        default=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
    )
    parser.add_argument(
        "--models",
        nargs="+",
        default=[
            # 推奨 (多言語、CTI 用途に最適):
            "snowflake-arctic-embed2",
            "nomic-embed-text-v2-moe",
            "granite-embedding",
            # 主に英語 (日英クロスリンガルに弱い可能性):
            "nomic-embed-text",
            "mxbai-embed-large",
        ],
    )
    args = parser.parse_args()
    print(f"Pairs: {len(PAIRS)}")
    print(f"Models: {args.models}")
    for model in args.models:
        r = await run_one(model, args.base_url)
        print_result(r)


if __name__ == "__main__":
    asyncio.run(main())
