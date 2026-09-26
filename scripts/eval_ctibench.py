#!/usr/bin/env python3
"""CTIBench でモデル自体の CTI 能力を測る (2026-09-26、文献調査 ② に基づく)。

出力の精度 (要点照合) とは別軸。測るのは 2 つ:
- CTI-ATE (60 問): ATT&CK テクニック抽出。main technique の ID 集合で micro-F1。
  kuebiko は記事から TTP を抽出している (ttp 34,217 件) が精度を一度も測っていない
  ⚠ 単純化ベンチの数値は実務より高く出る (Ryan et al. arXiv 2606.18166: 実務条件で micro-F1 0.22)
- CTI-MCQ (固定の抽出 N 問): 知識の選択問題。LoRA の前後 (base → s17 → s20) で比べ、特化による
  知識の劣化を見張る (前例: Foundation-Sec-8B の MMLU -2.4pt)。絶対値でなく **前後の差** を見る
  (ベンチの事前学習混入がありうるため)

データ: Hugging Face AI4Sec/cti-bench (NeurIPS 2024, Alam et al., CC-BY-NC-SA-4.0、内部評価のみ)。
data/mlx/ctibench/ に cti-ate.tsv / cti-mcq.tsv を置く。英語の問題なので、日本語出力の課題とは
形式が違う。

    uv run python scripts/eval_ctibench.py --model gemma4:26b --model kuebiko-sft:s17
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
import re
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.tools.llm_client import LLMError, OllamaClient  # noqa: E402

_TECH_RE = re.compile(r"\bT\d{4}\b")
_LETTER_RE = re.compile(r"\b([ABCD])\b")
_MCQ_SEED = 20260926  # 抽出を固定する (前後比較は同じ問題で)


def extract_ids(text: str) -> set[str]:
    """最終行 (空でない最後の行) の main technique ID。サブテクニック (.001) は本体に畳む。"""
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    last = lines[-1] if lines else ""
    return set(_TECH_RE.findall(last))


def extract_letter(text: str) -> str | None:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    for ln in reversed(lines):
        found = _LETTER_RE.findall(ln)
        if found:
            return found[-1]
    return None


def micro_f1(pairs: list[tuple[set[str], set[str]]]) -> dict[str, float]:
    """(予測, 正解) の組から micro 集計の precision / recall / F1。"""
    tp = sum(len(p & g) for p, g in pairs)
    fp = sum(len(p - g) for p, g in pairs)
    fn = sum(len(g - p) for p, g in pairs)
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return {"precision": prec, "recall": rec, "f1": f1}


def _read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


async def _ask(client: OllamaClient, prompt: str, max_tokens: int) -> str:
    try:
        res = await client.generate(
            prompt=prompt, temperature=0.0, max_tokens=max_tokens, think=False
        )
        return res.text
    except LLMError as e:
        return f"__error__ {type(e).__name__}"


async def run_model(
    model: str, ate: list[dict[str, str]], mcq: list[dict[str, str]]
) -> dict[str, Any]:
    client = OllamaClient(model=model, timeout_seconds=600.0)
    pairs: list[tuple[set[str], set[str]]] = []
    for i, row in enumerate(ate, 1):
        out = await _ask(client, row["Prompt"], 2000)
        gold = set(_TECH_RE.findall(row["GT"]))
        pairs.append((extract_ids(out), gold))
        print(f"{model} ATE {i}/{len(ate)}", flush=True)
    correct = 0
    for i, row in enumerate(mcq, 1):
        out = await _ask(client, row["Prompt"], 800)
        correct += extract_letter(out) == row["GT"].strip()
        if i % 50 == 0:
            print(f"{model} MCQ {i}/{len(mcq)} 正答 {correct}", flush=True)
    return {
        "model": model,
        "ate": micro_f1(pairs),
        "mcq_acc": correct / len(mcq) if mcq else 0.0,
        "mcq_n": len(mcq),
    }


async def main_async(args: argparse.Namespace) -> int:
    base = Path(args.data)
    ate = _read_tsv(base / "cti-ate.tsv")
    mcq_all = _read_tsv(base / "cti-mcq.tsv")
    mcq = random.Random(_MCQ_SEED).sample(mcq_all, min(args.mcq_n, len(mcq_all)))
    out_path = Path(args.out)
    results: dict[str, Any] = (
        json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
    )
    for model in args.model:
        if model in results:
            continue
        results[model] = await run_model(model, ate, mcq)
        out_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n=== CTIBench ===")
    for m, r in results.items():
        a = r["ate"]
        print(
            f"{m:24s} ATE micro-F1 {a['f1']:.3f} (P {a['precision']:.3f} / R {a['recall']:.3f})"
            f"  MCQ {r['mcq_acc']:.1%} (n={r['mcq_n']})"
        )
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--model", action="append", required=True)
    p.add_argument("--data", default="data/mlx/ctibench")
    p.add_argument("--mcq-n", type=int, default=500)
    p.add_argument("--out", default="data/mlx/ctibench_results.json")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
