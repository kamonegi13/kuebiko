#!/usr/bin/env python3
"""Gemma 4 26B-A4B の LoRA 学習 (クラウド GPU 用、PyTorch + PEFT、2026-09-27)。

MLX (data/mlx/run_s20.sh) の学習と **損失の配分・学習率の形を揃える**:
- batch 1 の例ごとの平均 → 勾配蓄積で均等に平均
  (mlx-lm 0.31 の default_loss と同じ。課題の重み = 例数)
- プロンプト部は損失から外す (mask-prompt)。教師出力の位置だけ logits を作る
  (語彙 26 万 × 12k tok を避ける)
- 学習率: 線形 warmup → cosine で lr_end まで
- LoRA: 注意機構・密 MLP の Linear と、専門家の 3 次元の重み
  (target_parameters = 専門家ごとの LoRA)。
  ⚠ HF の専門家は gate と up が 1 本 (gate_up_proj) なので、LoRA も gate/up で共有 (MLX は別々)
- mlx の LoRA ``scale`` は直接の乗数。PEFT では lora_alpha = scale × rank で同じ倍率になる

出力 (``--out``): adapter/ (PEFT 形式)・metrics.jsonl (更新ごと)・DONE.json
"""

from __future__ import annotations

import argparse
import json
import math
import random
import time
from pathlib import Path
from typing import Any

import torch
from torch.nn import functional


def load_examples(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.open() if x.strip()]


#: transformers の Gemma 4 テンプレートは、思考なしの生成開始に空の思考欄を付ける。本番 (Ollama の
#: RENDERER gemma4 = emptyBlockOnNothink 無効) と MLX の学習はこれを付けないので、外して揃える
#: (2026-09-27 に Ollama のソースと MLX の境界で確認。揃えないと学習と本番の入力がずれる)
_EMPTY_THOUGHT = "<|channel>thought\n<channel|>"


def encode(tok: Any, messages: list[dict[str, str]], max_seq: int) -> tuple[list[int], int] | None:
    """(token 列, 教師出力が始まる位置)。上限超え・プロンプトが前方一致しない例は None (除外)。"""
    full_txt = tok.apply_chat_template(messages, tokenize=False)
    prompt_txt = tok.apply_chat_template(messages[:-1], tokenize=False, add_generation_prompt=True)
    if prompt_txt.endswith(_EMPTY_THOUGHT) and not full_txt.startswith(prompt_txt):
        prompt_txt = prompt_txt[: -len(_EMPTY_THOUGHT)]
    if not full_txt.startswith(prompt_txt):
        return None
    full = tok(full_txt, add_special_tokens=False)["input_ids"]
    prompt = tok(prompt_txt, add_special_tokens=False)["input_ids"]
    if len(full) > max_seq or full[: len(prompt)] != prompt or len(prompt) >= len(full):
        return None
    return list(full), len(prompt)


def lr_at(update: int, *, lr: float, lr_end: float, warmup: int, total: int) -> float:
    if update < warmup:
        return lr * (update + 1) / warmup
    progress = (update - warmup) / max(1, total - warmup)
    return lr_end + 0.5 * (lr - lr_end) * (1 + math.cos(math.pi * min(1.0, progress)))


def build_peft(model: Any, args: argparse.Namespace) -> Any:
    from peft import LoraConfig, get_peft_model

    n_layers = args.total_layers
    layers = list(range(n_layers - args.num_layers, n_layers))
    lp = "|".join(map(str, layers))
    mods = r"self_attn\.(q|k|v|o)_proj|mlp\.(gate|up|down)_proj" + (
        r"|router\.proj" if args.router else ""
    )
    target_modules = rf"model\.language_model\.layers\.({lp})\.({mods})"
    target_parameters = [
        f"model.language_model.layers.{i}.experts.{p}"
        for i in layers
        for p in ("gate_up_proj", "down_proj")
    ]
    cfg = LoraConfig(
        r=args.rank,
        lora_alpha=args.scale * args.rank,
        lora_dropout=0.0,
        target_modules=target_modules,
        target_parameters=target_parameters,
    )
    return get_peft_model(model, cfg)


def example_loss(model: Any, ids: list[int], start: int, device: str) -> torch.Tensor:
    """1 例の損失 = 教師出力トークンの CE の平均 (例ごとの平均)。"""
    input_ids = torch.tensor([ids], device=device)
    # 位置 p の logits が token p+1 を予測する。
    # 教師出力 [start, len) を予測する位置は [start-1, len-1)
    keep = torch.arange(start - 1, len(ids) - 1, device=device)
    out = model(input_ids=input_ids, logits_to_keep=keep, use_cache=False)
    logits = out.logits[0].float()
    targets = input_ids[0, start:]
    return functional.cross_entropy(logits, targets)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="google/gemma-4-26B-A4B-it")
    ap.add_argument(
        "--data", type=Path, required=True, help="train.jsonl / valid.jsonl のあるディレクトリ"
    )
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--rank", type=int, default=32)
    ap.add_argument("--scale", type=float, default=10.0)
    ap.add_argument("--num-layers", type=int, default=30)
    ap.add_argument("--total-layers", type=int, default=30)
    ap.add_argument(
        "--router", action="store_true", help="router にも LoRA (s20 は有り。v2 は無し)"
    )
    ap.add_argument("--lr", type=float, default=3e-5)
    ap.add_argument("--lr-end", type=float, default=3e-6)
    ap.add_argument("--warmup", type=int, default=60)
    ap.add_argument("--accum", type=int, default=4)
    ap.add_argument("--epochs", type=float, default=1.0)
    ap.add_argument("--max-seq", type=int, default=12500)
    ap.add_argument(
        "--max-updates", type=int, default=0, help=">0 なら試験走行 (速度とメモリの実測)"
    )
    ap.add_argument("--save-every", type=int, default=250)
    ap.add_argument("--eval-every", type=int, default=250)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--dtype", default="bfloat16")
    ap.add_argument("--model-config", type=Path, help="試験用: 設定から乱数初期化 (重みを読まない)")
    args = ap.parse_args()

    from transformers import AutoConfig, AutoModelForImageTextToText, AutoTokenizer

    random.seed(args.seed)
    torch.manual_seed(args.seed)
    args.out.mkdir(parents=True, exist_ok=True)
    dtype = getattr(torch, args.dtype)
    tok = AutoTokenizer.from_pretrained(args.model)
    if args.model_config:
        cfg = AutoConfig.from_pretrained(args.model_config)
        model = AutoModelForImageTextToText.from_config(cfg, dtype=dtype).to(args.device)
    else:
        model = AutoModelForImageTextToText.from_pretrained(
            args.model, dtype=dtype, device_map=args.device
        )
    model.config.use_cache = False
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.enable_input_require_grads()
    model = build_peft(model, args)
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(json.dumps({"trainable_params": trainable}), flush=True)

    train = [
        e
        for e in (
            encode(tok, r["messages"], args.max_seq)
            for r in load_examples(args.data / "train.jsonl")
        )
        if e
    ]
    valid_path = args.data / "valid.jsonl"
    valid = (
        [
            e
            for e in (encode(tok, r["messages"], args.max_seq) for r in load_examples(valid_path))
            if e
        ]
        if valid_path.exists()
        else []
    )
    order: list[int] = []
    for ep in range(math.ceil(args.epochs)):
        idx = list(range(len(train)))
        random.Random(args.seed + ep).shuffle(idx)
        order += idx
    order = order[: int(len(train) * args.epochs)]
    total_updates = len(order) // args.accum
    if args.max_updates:
        total_updates = min(total_updates, args.max_updates)
    print(
        json.dumps({"examples": len(train), "valid": len(valid), "updates": total_updates}),
        flush=True,
    )
    if total_updates == 0:
        # 例が 0 件のまま「完了」と書かない (2026-09-27 の試験で型の取り違えにより全件除外された)
        raise SystemExit("学習例が 0 件 — encode がすべて除外した。テンプレート/トークナイザを確認")

    params = [p for p in model.parameters() if p.requires_grad]
    opt = torch.optim.AdamW(params, lr=args.lr, weight_decay=0.0)
    model.train()
    t0 = time.time()
    seen_tokens = 0
    metrics = (args.out / "metrics.jsonl").open("a", encoding="utf-8")
    for u in range(total_updates):
        for g in opt.param_groups:
            g["lr"] = lr_at(
                u, lr=args.lr, lr_end=args.lr_end, warmup=args.warmup, total=total_updates
            )
        losses = []
        for k in range(args.accum):
            ids, start = train[order[u * args.accum + k]]
            loss = example_loss(model, ids, start, args.device)
            (loss / args.accum).backward()  # 勾配蓄積は均等平均 (mlx と同じ)
            losses.append(loss.item())
            seen_tokens += len(ids)
        torch.nn.utils.clip_grad_norm_(params, 1.0)
        opt.step()
        opt.zero_grad(set_to_none=True)
        rec: dict[str, Any] = {
            "update": u + 1,
            "loss": sum(losses) / len(losses),
            "lr": opt.param_groups[0]["lr"],
            "tok_per_s": round(seen_tokens / (time.time() - t0), 1),
            "elapsed_s": round(time.time() - t0, 1),
        }
        if args.device.startswith("cuda"):
            rec["peak_mem_gb"] = round(torch.cuda.max_memory_allocated() / 1e9, 1)
        if valid and args.eval_every and (u + 1) % args.eval_every == 0:
            model.eval()
            with torch.no_grad():
                rec["valid_loss"] = sum(
                    example_loss(model, i, s, args.device).item() for i, s in valid
                ) / len(valid)
            model.train()
        metrics.write(json.dumps(rec) + "\n")
        metrics.flush()
        if (u + 1) % 10 == 0 or u == 0:
            print(json.dumps(rec), flush=True)
        if args.save_every and (u + 1) % args.save_every == 0:
            model.save_pretrained(args.out / "adapter-last")
    model.save_pretrained(args.out / "adapter")
    done = {
        "status": "done",
        "updates": total_updates,
        "seconds": round(time.time() - t0, 1),
        "tokens": seen_tokens,
        "trainable_params": trainable,
        "args": {k: str(v) for k, v in vars(args).items()},
    }
    (args.out / "DONE.json").write_text(json.dumps(done, ensure_ascii=False, indent=1))
    print(json.dumps(done), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
