#!/usr/bin/env python3
"""PIR 別の要点 (PIR_DAILY_FOCUS) の SFT 教師対を外部 LLM で作る (2026-09-29)。

背景: 朝ブリーフの PIR 別の要点は s21 が担当しているが、**学習していない課題**だった
(docs/research/llm_training/next_models_s22_n20.md §3)。s22 へ足す。

入力は本番と同じ: 各 PIR の 1 日分の照合結果 (medium 以上) から上位 3 件を選び、本番の
プロンプト (``pir_daily_focus._build_prompt``) を組む。過去の日を再現するため、長い窓で
照合を 1 回取り、記事の日付 (JST) で日ごとに分ける。**DB には何も書かない** (照合は読み取りのみ。
過去日付で本番の段を再生して台帳を汚した 09-06 の二の舞を避ける)。

⚠ PIR の定義は収穫時点のもの (過去の日の定義ではない)。要点は「その PIR から見て何を押さえるか」
なので、今の定義で書かせる方が本番の入力に近い。

    docker exec -e LLM_LOCAL_FALLBACK=0 kuebiko python \\
        scripts/build_sft_teacher_pir_focus.py --model claudecode:opus --days 60 --max 300
出力: data/mlx/teacher/pir_daily_focus.jsonl (再開可能)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.config_loader import load_app_config  # noqa: E402
from src.digest.pir_daily_focus import (  # noqa: E402
    _LLM_MAX_TOKENS,
    _MAX_ARTICLES_PER_PIR,
    _build_prompt,
    _filter_meaningful_matches,
    _sort_matches,
)
from src.pir.evaluator import PirMatch, evaluate_pir_matches  # noqa: E402
from src.pir.integration import load_current_pir_config  # noqa: E402
from src.tools.llm_client import LLMClient, LLMError  # noqa: E402
from src.tools.model_tiers import Step, build_llm_for_ref  # noqa: E402

OUT = Path("data/mlx/teacher/pir_daily_focus.jsonl")
_JST = ZoneInfo("Asia/Tokyo")
#: 1 PIR あたりの上限 (照合の多い PIR が教師を占めないように)
_PER_PIR_MAX = 25
_SEED = 929
_CONCURRENCY = 2
#: 本番の上限 (256) は要点 1-2 文の長さ。教師は同じ長さで書くので余裕だけ持たせる
_TEACHER_MAX_TOKENS = max(_LLM_MAX_TOKENS, 600)


def _day(m: PirMatch) -> str:
    try:
        ts = datetime.fromisoformat(m.created_at.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return ts.astimezone(_JST).date().isoformat()


def windows(days: int) -> list[tuple[str, Any, list[PirMatch]]]:
    """(key, pir, 上位 3 件) の列。key = ``{pir_id}:{日付}``。"""
    out: list[tuple[str, Any, list[PirMatch]]] = []
    for pir in (p for p in load_current_pir_config().priorities if p.enabled):
        matches = _filter_meaningful_matches(
            evaluate_pir_matches(pir, lookback_hours=days * 24, limit=20000)
        )
        by_day: dict[str, list[PirMatch]] = defaultdict(list)
        for m in matches:
            if d := _day(m):
                by_day[d].append(m)
        for d, ms in sorted(by_day.items()):
            out.append((f"{pir.id}:{d}", pir, _sort_matches(ms)[:_MAX_ARTICLES_PER_PIR]))
    return out


def sample(items: list[tuple[str, Any, list[PirMatch]]], total: int) -> list[Any]:
    """PIR ごとに上限をかけて無作為に選ぶ (照合の多い PIR に偏らない)。"""
    rng = random.Random(_SEED)
    by_pir: dict[str, list[Any]] = defaultdict(list)
    for it in items:
        by_pir[it[1].id].append(it)
    picked = [
        x for group in by_pir.values() for x in rng.sample(group, min(_PER_PIR_MAX, len(group)))
    ]
    rng.shuffle(picked)
    return picked[:total]


async def _one(llm: LLMClient, sem: asyncio.Semaphore, item: Any) -> dict[str, Any] | None:
    key, pir, top = item
    prompt = _build_prompt(pir, top)
    async with sem:
        try:
            res = await llm.generate(
                prompt=prompt, temperature=0.0, max_tokens=_TEACHER_MAX_TOKENS, think=False
            )
        except LLMError as exc:
            print(f"{key} 失敗 {type(exc).__name__}", flush=True)
            return None
    text = res.text.strip()
    if not text:
        return None
    return {
        "key": key,
        "pir_id": pir.id,
        "n_matches": len(top),
        "prompt": prompt,
        "completion": text,
    }


async def main_async(args: argparse.Namespace) -> int:
    llm = build_llm_for_ref(args.model, Step.PIR_DAILY_FOCUS, load_app_config())
    print(f"教師モデル: {llm.model}", flush=True)
    done = (
        {json.loads(x)["key"] for x in OUT.read_text(encoding="utf-8").splitlines() if x.strip()}
        if OUT.exists()
        else set()
    )
    items = windows(args.days)
    picked = [it for it in sample(items, args.max) if it[0] not in done]
    print(f"窓 {len(items)} / 選択 {args.max} / 既存 {len(done)} / 今回 {len(picked)}", flush=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(_CONCURRENCY)
    n = 0
    with OUT.open("a", encoding="utf-8") as fh:
        for fut in asyncio.as_completed([_one(llm, sem, it) for it in picked]):
            row = await fut
            if row:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
                n += 1
                if n % 25 == 0:
                    print(f"  {n} 件", flush=True)
    print(f"書込 {n} 件: {OUT}", flush=True)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", default="claudecode:opus")
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--max", type=int, default=300)
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
