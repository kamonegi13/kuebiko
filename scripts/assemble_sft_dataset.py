#!/usr/bin/env python3
"""多タスク SFT の学習データを組み立てる (事象ニュース + article_summary + triage)。

単一タスクで学習した v1 は、**学習時に見ていないスキーマ**で縮退した (同一 ID の重複
56.8% / 空欄に文字列 "null")。よって複数課題を混ぜ、どの課題かはプロンプトに教えさせる。

不変条件 (どれも v1 の失敗から来ている):
- **切り詰めない。上限を超える標本は除外する**。mlx-lm の「上限ちょうどへの切り詰め」が
  学習中の NaN を起こした実績がある (2026-09-03)。
- **文字数をトークン数の代理にしない**。実トークン数をチャットテンプレート込みで数える。
  文字数で切ったとき、日本語の換算誤差で completion 全切断の標本が残り 0/0 損失になった。
- **「正しく空にする」例が各課題に含まれていることを確認してから出す**。v1 の "null"
  欠陥はこの例を一度も見ていないことが原因なので、ここを検査しないと再発する。

使用例:
    data/mlx/venv/bin/python scripts/assemble_sft_dataset.py --out-dir data/mlx/dataset_v2
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# v1 の最終構成に合わせる (seq 12288 で学習し、合計 12,000 tok 超は除外した)。
_MAX_TOKENS = 12000

# 事象ニュースの 2 書式を見分けるための目印 (プロンプト冒頭の文言)。
_EVENTNEWS_MARKERS = ("同一事象", "1 つの事象を報じている 1 件の記事")


def _load_pairs(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        if d.get("prompt") and d.get("completion"):
            out.append({"prompt": d["prompt"], "completion": d["completion"]})
    return out


def _task_of(pair: dict[str, str]) -> str:
    prompt = pair["prompt"]
    if any(m in prompt for m in _EVENTNEWS_MARKERS):
        return "eventnews"
    if "重要度判定" in prompt:
        return "triage"
    return "article_summary"


def _empty_field_count(completion: str) -> int:
    """completion のうち「正しく空」になっている欄の数。"""
    try:
        payload = json.loads(completion)
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0
    return sum(1 for v in payload.values() if v is None or v == [] or v == "")


def _token_len(tokenizer: Any, pair: dict[str, str]) -> int:
    """チャットテンプレート込みの実トークン数 (学習時と同じ数え方)。"""
    try:
        rendered = tokenizer.apply_chat_template(
            [{"role": "user", "content": pair["prompt"]}],
            tokenize=False,
            add_generation_prompt=True,
        )
    except Exception:  # noqa: BLE001 — テンプレート未提供の tokenizer は素の連結で数える
        rendered = pair["prompt"]
    return len(tokenizer.encode(rendered)) + len(tokenizer.encode(pair["completion"]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--eventnews", type=Path, default=Path("data/mlx/dataset"))
    ap.add_argument("--summary", type=Path, default=Path("data/mlx/teacher/article_summary.jsonl"))
    ap.add_argument("--triage", type=Path, default=Path("data/mlx/teacher/triage.jsonl"))
    ap.add_argument("--out-dir", type=Path, default=Path("data/mlx/dataset_v2"))
    ap.add_argument("--model", default="mlx-community/gemma-4-26b-a4b-it-8bit")
    ap.add_argument("--valid-size", type=int, default=60)
    ap.add_argument("--seed", type=int, default=13)
    args = ap.parse_args()

    pairs: list[dict[str, str]] = []
    pairs += _load_pairs(args.eventnews / "train.jsonl")
    pairs += _load_pairs(args.eventnews / "valid.jsonl")
    pairs += _load_pairs(args.summary)
    pairs += _load_pairs(args.triage)
    if not pairs:
        print("入力が空", file=sys.stderr)
        return 1

    from mlx_lm.utils import load  # 遅延 import (mlx は arm64 venv のみ)

    _, tokenizer = load(args.model)

    kept: list[dict[str, str]] = []
    dropped: Counter[str] = Counter()
    for pair in pairs:
        task = _task_of(pair)
        n = _token_len(tokenizer, pair)
        if n > _MAX_TOKENS:
            dropped[task] += 1
            continue
        kept.append(pair)

    by_task = Counter(_task_of(p) for p in kept)
    print("=== 課題ごとの構成 ===")
    total = len(kept)
    for task, n in by_task.most_common():
        empties = sum(
            1 for p in kept if _task_of(p) == task and _empty_field_count(p["completion"])
        )
        print(
            f"  {task:16s} {n:5d} 件 ({100 * n / total:4.1f}%) "
            f"/ 上限超過で除外 {dropped[task]:3d} / **空欄を含む例 {empties:4d} 件**"
        )

    # 「正しく空にする」例が無い課題があると v1 の "null" 欠陥が再発する。
    missing = [
        t
        for t in by_task
        if not any(_task_of(p) == t and _empty_field_count(p["completion"]) for p in kept)
    ]
    if missing:
        print(
            f"\n⚠ 空欄の例が 1 件も無い課題: {missing} — この状態で学習すると再発する",
            file=sys.stderr,
        )

    random.Random(args.seed).shuffle(kept)
    valid, train = kept[: args.valid_size], kept[args.valid_size :]
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in (("train", train), ("valid", valid)):
        path = args.out_dir / f"{name}.jsonl"
        path.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
        )
        print(f"\n{path}: {len(rows)} 件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
