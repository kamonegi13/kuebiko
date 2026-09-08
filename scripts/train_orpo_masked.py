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
from mlx.utils import tree_flatten
from mlx_lm.tuner.utils import linear_to_lora_layers
from mlx_lm.utils import load
from mlx_lm_lora.trainer.orpo_trainer import (
    ORPOTrainingArgs,
    evaluate_orpo,
    orpo_loss,
    train_orpo,
)

PAD_TO = 8

# 記憶済み chosen (継続学習では自明) は平均 logp → 0 に張り付き、_log1mexp の勾配
# (-e^x/(1-e^x)) が -1e7 級に爆発 → iter 1 で重みが NaN 化する。損失側の nan_to_num が
# それを 0 に潰すため **loss は β·ln2 = 0.0693 に固定表示され NaN が見えない** (2026-09-08
# に n2p 全 42 キー NaN で発覚。09-07 の「loss 0.069 固定・全指標 0」も同一現象)。
# 対策: logp を -_LOGP_CEIL 以下へクランプしてから上流 orpo_loss へ渡す。
_LOGP_CEIL = 0.01


def stable_orpo_loss(
    chosen_logps: Any,
    chosen_logits_mean: Any,
    rejected_logps: Any,
    rejected_logits_mean: Any,
    chosen_masks: Any,
    rejected_masks: Any,
    preference_scores: Any,
    beta: float = 0.1,
) -> Any:
    """orpo_loss と同シグネチャ (train_orpo の loss= に渡す)。logp を 0 から引き離す。

    クランプ域では chosen 側の勾配が 0 になる — 記憶済み chosen から学ぶものは無く、
    rejected の押し下げ (選好の本体) はそのまま生きる。
    """
    chosen_logps = mx.minimum(chosen_logps, mx.array(-_LOGP_CEIL))
    rejected_logps = mx.minimum(rejected_logps, mx.array(-_LOGP_CEIL))
    return orpo_loss(
        chosen_logps=chosen_logps,
        chosen_logits_mean=chosen_logits_mean,
        rejected_logps=rejected_logps,
        rejected_logits_mean=rejected_logits_mean,
        chosen_masks=chosen_masks,
        rejected_masks=rejected_masks,
        preference_scores=preference_scores,
        beta=beta,
    )


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
    """prompt 領域を 0 にした mask を流す (それ以外は上流 iterate_orpo_batches と同形)。

    長い順に並べる (上流は短い順): 最長バッチが最初に来るため、メモリ包絡の超過が
    iter 1 で露見する (昇順 + シャッフルだと数十 iter 先で不意に OOM する)。
    """
    idx = sorted(range(len(dataset)), key=lambda i: len(dataset[i]["chosen"]), reverse=True)
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


def _train_self(
    model: Any, optimizer: Any, train_set: MaskedORPODataset, args: argparse.Namespace
) -> int:
    """自前 ORPO ループ。mlx_lm の SFT と同じ逆伝播経路 (全グラフ value_and_grad)。"""
    import mlx.nn as nn
    from mlx_lm.tuner.trainer import grad_checkpoint

    layers = model.model.layers if hasattr(model, "model") else model.layers
    grad_checkpoint(layers[0])

    def loss_fn(mdl: Any, c: Any, r: Any, cm: Any, rm: Any) -> Any:
        def avg_logp(tokens: Any, mask: Any) -> Any:
            logits = mdl(tokens[:, :-1]).astype(mx.float32)
            lp = -nn.losses.cross_entropy(logits, tokens[:, 1:], reduction="none")
            # softmax アンダーフローの -inf を遮断 (上流 get_logps と同じガード。
            # これを欠くと iter 1 の forward から NaN になる — 2026-09-08 実測)
            lp = mx.clip(lp, -1000.0, 0.0)
            m = mask[:, :-1]
            return (lp * m).sum(-1) / mx.maximum(m.sum(-1), 1.0)

        c_lp = mx.minimum(avg_logp(c, cm), mx.array(-_LOGP_CEIL))
        r_lp = mx.minimum(avg_logp(r, rm), mx.array(-_LOGP_CEIL))
        nll = -c_lp
        log1m = lambda x: mx.log(-mx.expm1(mx.minimum(x, mx.array(-1e-6))))  # noqa: E731
        log_odds = (c_lp - r_lp) - (log1m(c_lp) - log1m(r_lp))
        loss = nll - args.beta * nn.log_sigmoid(log_odds)
        return mx.mean(loss), (mx.mean(c_lp), mx.mean(r_lp))

    vag = nn.value_and_grad(model, loss_fn)
    it = 0
    for batch in iterate_masked_orpo_batches(
        train_set, args.batch_size, args.max_seq_length, train=True
    ):
        c, r, cm, rm, _scores = batch
        (loss, (c_lp, r_lp)), grads = vag(model, c, r, cm, rm)
        optimizer.update(model, grads)
        mx.eval(model.parameters(), optimizer.state, loss)
        it += 1
        lv = float(loss)
        if lv != lv:  # NaN — 隠さず即中断 (nan_to_num の隠蔽が今回の教訓)
            print(f"Iter {it}: loss NaN — 中断", flush=True)
            return 1
        if it % 10 == 0 or it == 1:
            print(
                f"Iter {it}: loss {lv:.4f} / c_lp {float(c_lp):.4f} / r_lp {float(r_lp):.4f} "
                f"/ margin {float(c_lp - r_lp):+.4f} / peak {mx.get_peak_memory() / 1e9:.1f}GB",
                flush=True,
            )
        if it % args.save_every == 0 or it >= args.iters:
            weights = dict(tree_flatten(model.trainable_parameters()))
            mx.save_safetensors(str(args.adapter_path / "adapters.safetensors"), weights)
        if it >= args.iters:
            break
    print(f"自前ループ完了: {it} iters", flush=True)
    return 0


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
    ap.add_argument(
        "--engine",
        choices=["self", "upstream"],
        default="self",
        help="self = 自前ループ (mlx_lm 同型逆伝播、既定) / upstream = mlx-lm-lora (NaN 前歴)",
    )
    ap.add_argument(
        "--train-layers",
        type=int,
        default=0,
        help="勾配を流す層数 (0 = num-layers 全部)。v1 の 8 層を全ロードしつつ"
        "末尾 2 層だけ学習する等、resume の完全性とメモリを両立させる",
    )
    args = ap.parse_args()

    mx.set_memory_limit(int(args.cap_gb * 1024**3))
    mx.set_wired_limit(int(args.cap_gb * 1024**3))

    model, tokenizer = load(args.model)
    model.freeze()
    linear_to_lora_layers(model, args.num_layers, {"rank": 8, "dropout": 0.0, "scale": 20.0})
    if args.resume_adapter_file is not None:
        # ⭐ dtype 正規化 (2026-09-08 分離実測): モデル本体は bf16 だが、継続元 adapter
        # ファイルの重みが fp16 で混入すると長系列 (≳5k) の forward が全 NaN になる
        # (fp16 の範囲 65504 を活性が超える)。読み込む adapter 側だけを bf16 へ cast する
        # — model.apply で全体を cast すると量子化 scale のカーネル経路が変わり
        # 学習が ~6 倍遅くなる (74s/iter を実測)。
        resumed = [
            (k, v.astype(mx.bfloat16) if mx.issubdtype(v.dtype, mx.floating) else v)
            for k, v in mx.load(str(args.resume_adapter_file)).items()
        ]
        model.load_weights(resumed, strict=False)
        print(f"adapter 継続: {args.resume_adapter_file} (adapter 側のみ bf16 正規化)")
    if args.train_layers and args.train_layers < args.num_layers:
        # LoRA を巻いた num_layers のうち、末尾 train_layers 以外は凍結する。
        # resume した adapter の全層は forward に効き続ける (部分 resume の黙落を防ぐ)。
        layers = model.model.layers if hasattr(model, "model") else model.layers
        for layer in layers[: len(layers) - args.train_layers]:
            layer.freeze()
        n_trainable = sum(v.size for _, v in tree_flatten(model.trainable_parameters()))
        print(f"学習対象を末尾 {args.train_layers} 層に限定 (trainable {n_trainable / 1e6:.1f}M)")

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
    if args.engine == "upstream":
        train_orpo(
            model=model,
            optimizer=optimizer,
            train_dataset=train_set,
            val_dataset=valid_set,
            loss=stable_orpo_loss,
            args=training_args,
            training_callback=None,
        )
    else:
        # 自前ループ (2026-09-08): mlx-lm-lora の chunked 逆伝播はこの MoE で NaN を
        # 量産し、loss 側の nan_to_num が β·ln2=0.0693 の定数表示に隠蔽していた。
        # mlx_lm の SFT と同じプリミティブ (nn.value_and_grad + 全グラフ forward) で
        # ORPO を素直に組む。NaN は隠さず即中断する。
        rc = _train_self(model, optimizer, train_set, args)
        if rc != 0:
            return rc

    if args.resume_adapter_file is not None and args.train_layers:
        # 上流は trainable のみ保存する → 凍結した継続元の層が adapter から欠落する。
        # 継続元の全層に学習済み層を上書きマージして完全な adapter を再保存する
        # (これを怠ると fuse 時に v1 の凍結 6 層が黙って消える)。
        base_weights = dict(mx.load(str(args.resume_adapter_file)))
        trained = dict(mx.load(str(args.adapter_path / "adapters.safetensors")))
        merged = {**base_weights, **trained}
        # mx.load は memory-map するため、読み元と同じパスへ直接保存すると read エラーに
        # なる。実体化してから一時ファイル経由の原子的 rename で書く。
        mx.eval(*merged.values())
        tmp = args.adapter_path / "adapters.merged.safetensors"
        mx.save_safetensors(str(tmp), merged)
        tmp.replace(args.adapter_path / "adapters.safetensors")
        print(f"継続元 {len(base_weights)} keys + 学習済み {len(trained)} keys → merge 保存")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
