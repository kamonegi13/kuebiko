#!/usr/bin/env python3
"""prompt mask 付き ORPO 学習 (N2 用、2026-09-08)。

mlx-lm-lora の orpo 経路は prompt を mask しない (ORPODataset は offset を持たず、
iterate_orpo_batches が全域 1 の mask を作る)。事象ペアでは prompt が 7-8 割を占め
(prompt 領域 NLL ~10.5 vs completion ~2.9 実測)、(a) SFT 項が prompt 再生産で埋まり
(b) 選好対比も希釈される — 中断実験の「loss 9.3 異常」の正体 (SYNTHESIS.md §28)。

本スクリプトは dataset と batch iterator だけを差し替え、loss (orpo_loss) と
学習ループ (train_orpo) は mlx-lm-lora のものを流用する:
- MaskedORPODataset: ChatDataset と同じ方式で prompt offset を捕獲
- iterate_masked_orpo_batches: offset より前を 0 にした mask を供給
- 行ごとの preference_score (β の per-task 変調に使える口) は素通し

使用例 (夜間・Ollama 完全停止で):
    data/mlx/venv/bin/python scripts/train_orpo_masked.py \\
        --model <fused-v1 の mlx dir> --data data/mlx/dpo/pairs_n8 \\
        --adapter-path data/mlx/adapters/n2p --iters 300 --beta 0.1 \\
        --max-seq-length 8000 --num-layers 2 --cap-gb 95
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import mlx.core as mx
import mlx.optimizers as optim
import numpy as np
from mlx_lm.tuner.utils import linear_to_lora_layers
from mlx_lm.utils import load
from mlx_lm_lora.trainer.orpo_trainer import ORPOTrainingArgs, evaluate_orpo, train_orpo

PAD_TO = 8


class MaskedORPODataset:
    """{prompt, chosen, rejected, system?} → (tokens, prompt_offset) の対。

    offset は ChatDataset と同じ方式 (base_messages + add_generation_prompt の長さ)。
    """

    def __init__(self, rows: list[dict[str, Any]], tokenizer: Any) -> None:
        self._items: list[dict[str, Any]] = []
        for d in rows:
            base = (
                [{"role": "system", "content": d["system"]}] if d.get("system") else []
            )
            base = [*base, {"role": "user", "content": d["prompt"]}]
            offset = len(tokenizer.apply_chat_template(base, add_generation_prompt=True))
            item: dict[str, Any] = {"preference_score": float(d.get("preference_score", 1.0))}
            for side in ("chosen", "rejected"):
                toks = tokenizer.apply_chat_template(
                    [*base, {"role": "assistant", "content": d[side]}]
                )
                item[side] = toks
                item[f"{side}_offset"] = min(offset, len(toks))
            self._items.append(item)

    def __getitem__(self, idx: int) -> dict[str, Any]:
        return self._items[idx]

    def __len__(self) -> int:
        return len(self._items)

    def process(self, d: dict[str, Any]) -> dict[str, Any]:
        return d


def iterate_masked_orpo_batches(
    dataset: MaskedORPODataset, batch_size: int, max_seq_length: int, train: bool = False
) -> Any:
    """prompt 領域を 0 にした mask を流す (それ以外は上流 iterate_orpo_batches と同形)。"""
    idx = sorted(range(len(dataset)), key=lambda i: len(dataset[i]["chosen"]))
    if len(dataset) < batch_size:
        raise ValueError(f"データ {len(dataset)} 行 < batch_size {batch_size}")
    batch_idx = [idx[i : i + batch_size] for i in range(0, len(idx) - batch_size + 1, batch_size)]
    while True:
        order = np.random.permutation(len(batch_idx)) if train else range(len(batch_idx))
        for i in order:
            batch = [dataset[j] for j in batch_idx[i]]
            max_len = min(
                max(max(len(x["chosen"]), len(x["rejected"])) for x in batch),
                max_seq_length,
            )
            width = PAD_TO * ((max_len + PAD_TO - 1) // PAD_TO)
            arrs = {}
            for side in ("chosen", "rejected"):
                arr = np.zeros((len(batch), width), np.int32)
                mask = np.zeros((len(batch), width), np.float32)
                for j, item in enumerate(batch):
                    n = min(len(item[side]), width)
                    arr[j, :n] = item[side][:n]
                    # ⭐ 差し替えの核心: prompt 領域 (offset より前) は loss に入れない
                    mask[j, item[f"{side}_offset"] : n] = 1.0
                arrs[side] = (mx.array(arr), mx.array(mask))
            scores = mx.array([x["preference_score"] for x in batch], mx.float32)
            yield (
                arrs["chosen"][0],
                arrs["rejected"][0],
                arrs["chosen"][1],
                arrs["rejected"][1],
                scores,
            )
        if not train:
            break


def _load_rows(path: Path) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True)
    ap.add_argument("--data", type=Path, required=True, help="train.jsonl/valid.jsonl のある dir")
    ap.add_argument("--adapter-path", type=Path, required=True)
    ap.add_argument("--iters", type=int, default=300)
    ap.add_argument("--batch-size", type=int, default=1)
    ap.add_argument("--num-layers", type=int, default=2)
    ap.add_argument("--learning-rate", type=float, default=5e-6)
    ap.add_argument("--beta", type=float, default=0.1)
    ap.add_argument("--max-seq-length", type=int, default=8000)
    ap.add_argument("--cap-gb", type=float, default=95.0)
    ap.add_argument("--steps-per-eval", type=int, default=100)
    ap.add_argument("--save-every", type=int, default=50)
    ap.add_argument("--dry-run", action="store_true", help="初期 loss/指標を 1 batch 測って終了")
    ap.add_argument(
        "--resume-adapter-file",
        type=Path,
        default=None,
        help="既存 adapter から継続 (N2 = v1 継続。layer 構成は当該 adapter と一致させること)",
    )
    args = ap.parse_args()

    mx.set_memory_limit(int(args.cap_gb * 1024**3))
    mx.set_wired_limit(int(args.cap_gb * 1024**3))

    model, tokenizer = load(args.model)
    model.freeze()
    linear_to_lora_layers(model, args.num_layers, {"rank": 8, "dropout": 0.0, "scale": 20.0})
    if args.resume_adapter_file is not None:
        model.load_weights(str(args.resume_adapter_file), strict=False)
        print(f"adapter 継続: {args.resume_adapter_file}")

    train_set = MaskedORPODataset(_load_rows(args.data / "train.jsonl"), tokenizer)
    valid_path = args.data / "valid.jsonl"
    valid_set = (
        MaskedORPODataset(_load_rows(valid_path), tokenizer) if valid_path.exists() else train_set
    )
    print(f"train {len(train_set)} / valid {len(valid_set)} 対")

    training_args = ORPOTrainingArgs(
        batch_size=args.batch_size,
        iters=args.iters,
        val_batches=min(8, len(valid_set)),
        steps_per_eval=args.steps_per_eval,
        steps_per_save=args.save_every,
        max_seq_length=args.max_seq_length,
        adapter_file=str(args.adapter_path / "adapters.safetensors"),
        grad_checkpoint=True,
        beta=args.beta,
    )

    # 上流の train_orpo は module-level iterate_orpo_batches を直接呼ぶため、差し替えは
    # module 属性の付け替えで行う (vendored copy を持たず上流の loss/loop を使い続けるため)
    import mlx_lm_lora.trainer.orpo_trainer as orpo_mod

    orpo_mod.iterate_orpo_batches = iterate_masked_orpo_batches
    # 上流 train_orpo は冒頭で mx.set_wired_limit(max_recommended...) を呼び cap を
    # 上書きする (wired 暴走 = 2026-09-07 のマシンクラッシュ経路)。自分の cap を先に
    # 設定済みなので、以後の set_wired_limit は無効化して cap を守る。
    orpo_mod.mx.set_wired_limit = lambda *_a, **_k: None  # type: ignore[assignment]

    if args.dry_run:
        metrics = evaluate_orpo(
            model=model,
            dataset=valid_set,
            batch_size=args.batch_size,
            num_batches=1,
            beta=args.beta,
            max_seq_length=args.max_seq_length,
        )
        loss = metrics[0] if isinstance(metrics, tuple) else metrics
        expect = "期待: completion NLL ≈ 2-4。9 台なら mask 不発"
        print(f"dry-run 初期 loss: {float(loss):.3f} ({expect})")
        return 0

    args.adapter_path.mkdir(parents=True, exist_ok=True)
    (args.adapter_path / "adapter_config.json").write_text(
        json.dumps(
            {
                "fine_tune_type": "lora",
                "num_layers": args.num_layers,
                "lora_parameters": {"rank": 8, "dropout": 0.0, "scale": 20.0},
                "orpo_masked": True,
                "beta": args.beta,
                "data": str(args.data),
            }
        )
    )
    optimizer = optim.Adam(learning_rate=args.learning_rate)
    train_orpo(
        model=model,
        optimizer=optimizer,
        train_dataset=train_set,
        val_dataset=valid_set,
        args=training_args,
        training_callback=None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
