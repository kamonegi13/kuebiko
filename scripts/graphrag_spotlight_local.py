#!/usr/bin/env python3
"""GraphRAG の Spotlight をローカルモデルで確かめる — 線を使いこなせるか (2026-09-29)。

Opus では線つきが 12:3 で勝った (graphrag_spotlight_compare.py)。
本番で書くのはローカル (n19、線つきの
入力は未学習) なので、同じ 15 窓の入力 (腕 A = 線なし / 腕 B = 線つき) を本番と同じ形 (接頭辞・
温度 0.3・上限 6144) で生成させる。対読は compare の ``--judge --src`` で行う。

⚠ GPU を使う。本番 (kuebiko) を止めてから流す。

    uv run python scripts/graphrag_spotlight_local.py --model kuebiko-sft:n19
出力: data/mlx/graphrag_spotlight_local_<model>.jsonl (compare と同じ形、再開可能)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.config_loader import load_app_config  # noqa: E402
from src.spotlight.generator import _LLMSpotlightOutput  # noqa: E402
from src.tools.llm_client import LLMClient, OllamaClient  # noqa: E402
from src.tools.model_tiers import Step  # noqa: E402
from src.tools.task_prefix import TaskPrefixClient  # noqa: E402

D = _ROOT / "data" / "mlx"
SRC = D / "graphrag_spotlight.jsonl"


async def arm(llm: LLMClient, prompt: str) -> str | None:
    try:
        out = await llm.generate_structured(
            prompt, schema=_LLMSpotlightOutput, temperature=0.3, max_tokens=6144, think=False
        )
    except Exception as exc:  # noqa: BLE001
        print("  失敗", type(exc).__name__, flush=True)
        return None
    return out.model_dump_json()


async def main_async(args: argparse.Namespace) -> int:
    cfg = load_app_config()
    os.environ["SFT_TASK_PREFIX_MODELS"] = args.model
    llm: LLMClient = TaskPrefixClient(
        OllamaClient(base_url=cfg.ollama_base_url, model=args.model, timeout_seconds=900.0),
        Step.PIR_SPOTLIGHT,
    )
    out_path = D / f"graphrag_spotlight_local_{args.model.split(':')[-1]}.jsonl"
    done = {json.loads(x)["key"] for x in out_path.open()} if out_path.exists() else set()
    rows = [json.loads(x) for x in SRC.open(encoding="utf-8")]
    with out_path.open("a", encoding="utf-8") as fh:
        for r in rows:
            if r["key"] in done:
                continue
            a = await arm(llm, r["prompt_a"])
            b = await arm(llm, r["prompt_b"])
            if a and b:
                row = {**{k: r[k] for k in ("key", "prompt_a", "prompt_b")}, "a": a, "b": b}
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
                print("済", r["key"], flush=True)
    print("書込", out_path)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", default="kuebiko-sft:n19")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
