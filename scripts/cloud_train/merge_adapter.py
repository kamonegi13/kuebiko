#!/usr/bin/env python3
"""PEFT のアダプタを元のモデル (HF 形式・bf16) に統合して保存する (手元の Mac、2026-09-27)。

クラウドで学習したアダプタ (約 4GB) だけを持ち帰り、統合は手元で行う
(GPU の課金を持ち帰りに使わない)。統合後は既存の経路と同じく
``ollama create --quantize int4`` で取り込む (scripts/cloud_train/README.md)。

⚠ PEFT の ``merge_and_unload`` は使わない。差分を bf16 に落としてから bf16 の重みへ足すため、
重みの刻み (相対 0.2-0.4%) の半分より小さい差分が **丸めで系統的に消える** (元の重みは bf16 の
格子上にあるので、小さい差分を足しても同じ格子点へ戻る)。2026-09-27 に試験走行のアダプタで
差分の 84-92% が消え、統合後の出力がアダプタを外した元のモデルと同じになった。
ここでは差分を float32 で足し、**確率的丸め** で bf16 に戻す (切り上げの確率 = 端数)。
要素ごとの誤差は残るが偏りがないので、行列積で合算すると差分の効果が期待値どおり残る。

元のモデルは、キャッシュ済みの MLX bf16 版を scripts/mlx_fused_to_hf.py で HF の名前に戻したもの。
統合はモデルを組み立てず safetensors を 1 枚ずつ書き換える (メモリは 1 枚分)。

    data/cloud-venv/bin/python scripts/cloud_train/merge_adapter.py \\
        --base data/cloud-base-hf --adapter data/cloud-runs/<run>/out/adapter \\
        --out data/cloud-merged/<run> [--check data/mlx/dataset_<x>/valid.jsonl]
"""

from __future__ import annotations

import argparse
import gc
import json
import shutil
from pathlib import Path
from typing import Any

import torch
from safetensors import safe_open
from safetensors.torch import save_file

_ADAPTER_PREFIX = "base_model.model."
#: 検査: 統合後の損失の下がり幅が、アダプタありの下がり幅の 8 割以上
CHECK_GAIN_RATIO = 0.8
#: 検査に使う検証例の数 (短い順。回答部分の損失を比べる)
CHECK_EXAMPLES = 5
_BF16_DROP_MASK = -65536  # 0xFFFF0000: float32 の下位 16 bit (bf16 で捨てる部分) を落とす


def stochastic_round_bf16(x: torch.Tensor, seed: int) -> torch.Tensor:
    """float32 → bf16 を確率的に丸める (期待値 = 元の値)。

    float32 のビット列の下位 16 bit に一様乱数を足してから切り捨てる。符号は最上位 bit にあり
    絶対値の部分だけに繰り上がるので、正負どちらでも絶対値が確率的に丸まる。
    """
    bits = x.contiguous().to(torch.float32).view(torch.int32)
    gen = torch.Generator().manual_seed(seed)
    noise = torch.randint(0, 1 << 16, bits.shape, dtype=torch.int32, generator=gen)
    return ((bits + noise) & _BF16_DROP_MASK).view(torch.float32).to(torch.bfloat16)


def _deltas(adapter: Path) -> tuple[dict[str, dict[str, torch.Tensor]], float]:
    """{元の重みの名前の候補: {"A", "B"}} と scaling。"""
    cfg = json.loads((adapter / "adapter_config.json").read_text(encoding="utf-8"))
    if cfg.get("use_rslora") or cfg.get("rank_pattern") or cfg.get("alpha_pattern"):
        raise SystemExit("rslora / rank_pattern / alpha_pattern は未対応")
    scaling = float(cfg["lora_alpha"]) / float(cfg["r"])
    pairs: dict[str, dict[str, torch.Tensor]] = {}
    with safe_open(str(adapter / "adapter_model.safetensors"), "pt") as f:
        for key in f.keys():  # noqa: SIM118 — safe_open は dict でない
            stem, _, tail = key.removeprefix(_ADAPTER_PREFIX).rpartition(".lora_")
            pairs.setdefault(stem, {})[tail.split(".")[0]] = f.get_tensor(key).float()
    return pairs, scaling


def _linear_delta(ab: dict[str, torch.Tensor], scaling: float) -> torch.Tensor:
    return (ab["B"] @ ab["A"]) * scaling


def _experts_delta(ab: dict[str, torch.Tensor], scaling: float, shape: torch.Size) -> torch.Tensor:
    """PEFT ParamWrapper.get_delta_weight と同じ式 (3 次元 = expert ごとの LoRA)。"""
    n_exp = shape[0]
    a = ab["A"].reshape(n_exp, -1, ab["A"].shape[-1])  # (experts, rank, in)
    b = ab["B"].reshape(ab["B"].shape[0], -1, n_exp)  # (out, rank, experts)
    # 重みの並びは (experts, out, in) か (experts, in, out)。形で決める (正方でなければ一意)
    eq = "o r e, e r i -> e o i" if shape[1] == b.shape[0] else "o r e, e r i -> e i o"
    return torch.einsum(eq, b, a) * scaling


def _targets(
    pairs: dict[str, dict[str, torch.Tensor]], names: set[str], shapes: dict[str, torch.Size]
) -> dict[str, list[dict[str, torch.Tensor]]]:
    """元の重みの名前 → 足す LoRA の一覧。

    expert の重みは 1 つのモジュールに 2 本 (gate_up_proj / down_proj) あり、PEFT は
    ``experts`` と ``experts.base_layer`` に入れ子で包む。どちらがどの重みかは形で決める。
    """
    out: dict[str, list[dict[str, torch.Tensor]]] = {}
    for stem, ab in pairs.items():
        if f"{stem}.weight" in names:
            out.setdefault(f"{stem}.weight", []).append(ab)
            continue
        module = stem.removesuffix(".base_layer")
        in_dim, out_dim = ab["A"].shape[-1], ab["B"].shape[0]
        hits = [
            n
            for n in names
            if n.startswith(f"{module}.")
            and n.count(".") == module.count(".") + 1
            and len(shapes[n]) == 3
            and {shapes[n][1], shapes[n][2]} == {in_dim, out_dim}
        ]
        if len(hits) != 1:
            raise SystemExit(f"アダプタ {stem} の統合先が決まらない: {hits}")
        out.setdefault(hits[0], []).append(ab)
    return out


def merge(base: Path, adapter: Path, out: Path, *, rounding: str) -> int:
    pairs, scaling = _deltas(adapter)
    shards = sorted(base.glob("*.safetensors"))
    shapes: dict[str, torch.Size] = {}
    for sh in shards:
        with safe_open(str(sh), "pt") as f:
            for k in f.keys():  # noqa: SIM118 — safe_open は dict でない
                shapes[k] = torch.Size(f.get_slice(k).get_shape())
    targets = _targets(pairs, set(shapes), shapes)
    out.mkdir(parents=True, exist_ok=True)
    merged = 0
    for sh in shards:
        tensors: dict[str, torch.Tensor] = {}
        with safe_open(str(sh), "pt") as f:
            meta = f.metadata()
            for k in f.keys():  # noqa: SIM118 — safe_open は dict でない
                w = f.get_tensor(k)
                if k in targets:
                    acc = w.float()
                    for ab in targets.pop(k):
                        d = (
                            _linear_delta(ab, scaling)
                            if w.dim() == 2
                            else _experts_delta(ab, scaling, w.shape)
                        )
                        acc += d
                    if w.dtype == torch.bfloat16 and rounding == "stochastic":
                        w = stochastic_round_bf16(acc, seed=merged + 1)
                    else:
                        w = acc.to(w.dtype)
                    merged += 1
                tensors[k] = w
        save_file(tensors, str(out / sh.name), metadata=meta)
        del tensors
        gc.collect()
    if targets:
        raise SystemExit(f"統合されずに残った: {sorted(targets)[:5]}")
    for p in base.iterdir():
        if p.suffix != ".safetensors" and p.is_file():
            shutil.copy2(p, out / p.name)
    return merged


def _check_examples(base_dir: Path, valid: Path) -> list[tuple[torch.Tensor, int]]:
    """学習と同じ形 (本番 Ollama think=False の形) の検証例。回答の開始位置つき。"""
    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(base_dir)
    rows = [json.loads(x) for x in valid.open(encoding="utf-8") if x.strip()]
    rows.sort(key=lambda r: sum(len(m["content"]) for m in r["messages"]))
    out = []
    for r in rows[:CHECK_EXAMPLES]:
        msgs = r["messages"]
        prompt = tok.apply_chat_template(msgs[:-1], tokenize=False, add_generation_prompt=True)
        p_ids = tok(prompt, add_special_tokens=False).input_ids
        a_ids = tok(msgs[-1]["content"] + "<turn|>", add_special_tokens=False).input_ids
        out.append((torch.tensor([p_ids + a_ids]), len(p_ids)))
    return out


def _loss(model: Any, examples: list[tuple[torch.Tensor, int]], device: str) -> float:
    total = 0.0
    with torch.no_grad():
        for ids, start in examples:
            ids = ids.to(device)
            logits = model(input_ids=ids).logits.float()[0]
            total += float(
                torch.nn.functional.cross_entropy(logits[start - 1 : -1], ids[0, start:])
            )
    return total / len(examples)


def check(base: Path, adapter: Path, out: Path, valid: Path) -> None:
    """統合後が「アダプタあり」の効果を再現しているか。

    回答部分の損失が、元のモデル → アダプタあり でどれだけ下がるかを測り、統合後も同じだけ
    下がることを確かめる。⚠ CPU の bf16 計算は精度が足りず検査にならない (2026-09-27 実測:
    同じモデルで損失が 0.1-0.15 ずれ、分布の距離はどの組でも同程度になった) → GPU (MPS) で測る。
    モデルは 1 つずつ載せる (26B の bf16 を 2 つ同時に載せない)。
    """
    from peft import PeftModel
    from transformers import AutoModelForImageTextToText

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    examples = _check_examples(base, valid)
    m = AutoModelForImageTextToText.from_pretrained(base, dtype=torch.bfloat16, device_map=device)
    pm = PeftModel.from_pretrained(m, adapter)
    l_lora = _loss(pm, examples, device)
    with pm.disable_adapter():
        l_orig = _loss(pm, examples, device)
    del pm, m
    gc.collect()
    if device == "mps":
        torch.mps.empty_cache()
    mm = AutoModelForImageTextToText.from_pretrained(out, dtype=torch.bfloat16, device_map=device)
    l_merged = _loss(mm, examples, device)
    del mm
    gc.collect()
    gain, gain_m = l_orig - l_lora, l_orig - l_merged
    print(
        f"回答の損失 ({device}, {len(examples)} 例): 元 {l_orig:.3f} / アダプタあり {l_lora:.3f} / "
        f"統合後 {l_merged:.3f}  (下がり幅 {gain:.3f} → 統合後 {gain_m:.3f})"
    )
    if not (gain > 0 and gain_m >= CHECK_GAIN_RATIO * gain):
        raise SystemExit("⚠ 統合後がアダプタの効果を再現していない")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", type=Path, required=True)
    ap.add_argument("--adapter", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--rounding", choices=("stochastic", "nearest"), default="stochastic")
    ap.add_argument(
        "--check",
        type=Path,
        metavar="VALID_JSONL",
        help="学習の検証データで、統合後がアダプタの効果を再現しているか確かめる",
    )
    args = ap.parse_args()
    n = merge(args.base, args.adapter, args.out, rounding=args.rounding)
    print(f"統合した重み {n} 本 → {args.out}")
    if args.check:
        try:
            check(args.base, args.adapter, args.out, args.check)
        except SystemExit:
            shutil.rmtree(args.out, ignore_errors=True)
            raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
