#!/usr/bin/env python3
"""SFT モデルを **本番と同じ経路** (Ollama + schema 制約) で評価する。

MLX 上の評価は制約なしデコードだったため JSON 妥当率が本番と無関係な数字になっていた
(本番は ``generate_structured`` が Ollama の ``format`` にスキーマを渡して制約する)。
ここでは production の ``OllamaClient.generate_structured`` をそのまま呼ぶ — 評価と
本番で「取得」が分かれると、処理を共有していても挙動は一致しないため。

入力は ``data/mlx/eval_sft.json`` (prompt / reference=Opus / generated=MLX bf16)。
同じ prompt を Ollama 経由の SFT モデルへ流し、充足軸を 3 者で比較する。

使用例:
    uv run python scripts/eval_sft_ollama.py --model kuebiko-sft:26b --limit 1
    uv run python scripts/eval_sft_ollama.py --model kuebiko-sft:26b \
        --out data/mlx/eval_ollama_sft.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.eventnews.models import EventNewsDraft  # noqa: E402
from src.tools.llm_client import OllamaClient  # noqa: E402

# 本番 generate_draft と同じ (src/eventnews/generator.py)。
_TEMPERATURE = 0.2
_THINK = False

# 26B の 1 件あたり実測は 15-60 秒。長い群では余裕を持たせる。
_TIMEOUT_SECONDS = 900.0

# 充足軸 (Opus 蒸留で伸ばした欄)。
_FILL_FIELDS = ("caveats", "discrepancies", "unknowns", "facts", "key_points")


def _parse(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


async def _run_one(client: OllamaClient, prompt: str) -> tuple[str | None, str | None]:
    """(生成 JSON, エラー) を返す。例外は握って比較を続ける。"""
    try:
        draft = await client.generate_structured(
            prompt=prompt,
            schema=EventNewsDraft,
            temperature=_TEMPERATURE,
            think=_THINK,
            max_attempts=1,  # 盲目リトライを切り、素の妥当率を測る
        )
    except Exception as exc:  # noqa: BLE001 - 評価継続のため型を問わず記録する
        return None, f"{type(exc).__name__}: {exc}"
    return draft.model_dump_json(), None


def _summarize(label: str, payloads: list[dict[str, Any] | None], total: int) -> None:
    valid = [p for p in payloads if p is not None]
    print(f"\n[{label}] JSON 妥当 {len(valid)}/{total}")
    if not valid:
        return
    for field in _FILL_FIELDS:
        values = [len(p.get(field) or []) for p in valid]
        print(f"  {field:14s} 平均 {statistics.mean(values):5.2f}  0 件 {values.count(0):3d}")


async def main_async(args: argparse.Namespace) -> int:
    records: list[dict[str, Any]] = json.loads(args.src.read_text(encoding="utf-8"))
    if args.limit:
        records = records[: args.limit]

    client = OllamaClient(model=args.model, timeout_seconds=_TIMEOUT_SECONDS)
    results: list[dict[str, Any]] = []
    started = time.monotonic()

    for i, record in enumerate(records, start=1):
        generated, error = await _run_one(client, record["prompt"])
        results.append({**record, "ollama_generated": generated, "ollama_error": error})
        elapsed = time.monotonic() - started
        status = "ok" if error is None else error[:60]
        print(f"  {i}/{len(records)} ({elapsed:.0f}s) {status}", flush=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n書き出し: {args.out}")

    total = len(results)
    _summarize("Opus 参照", [_parse(r.get("reference")) for r in results], total)
    _summarize("MLX bf16 (制約なし)", [_parse(r.get("generated")) for r in results], total)
    _summarize(
        "Ollama Q4 (schema 制約)", [_parse(r.get("ollama_generated")) for r in results], total
    )

    failures = [r for r in results if r.get("ollama_error")]
    if failures:
        print(f"\n生成エラー {len(failures)} 件: {failures[0]['ollama_error'][:200]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="Ollama のモデル名")
    parser.add_argument("--src", type=Path, default=Path("data/mlx/eval_sft.json"))
    parser.add_argument("--out", type=Path, default=Path("data/mlx/eval_ollama_sft.json"))
    parser.add_argument("--limit", type=int, default=0, help="先頭 N 件だけ流す (0 = 全件)")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
