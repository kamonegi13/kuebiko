#!/usr/bin/env python3
"""PEFT のアダプタを元のモデル (HF 形式・bf16) に統合して保存する (手元の Mac、2026-09-27)。

クラウドで学習したアダプタ (約 5GB) だけを持ち帰り、統合は手元で行う
(GPU の課金を持ち帰りに使わない)。統合後は既存の経路と同じく
``ollama create --quantize int4`` で取り込む (scripts/cloud_train/README.md)。

元のモデルは、キャッシュ済みの MLX bf16 版を scripts/mlx_fused_to_hf.py で HF の名前に戻したもの
(値は変えず名前と並びだけ戻す。52GB の再ダウンロードを避ける)。

    data/cloud-venv/bin/python scripts/cloud_train/merge_adapter.py \\
        --base data/cloud-base-hf --adapter data/cloud-runs/<run>/out/adapter \\
        --out data/cloud-merged/<run> [--check]
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import torch

_SIDECAR = (
    "tokenizer.json",
    "tokenizer_config.json",
    "chat_template.jinja",
    "special_tokens_map.json",
    "tokenizer.model",
    "generation_config.json",
    "processor_config.json",
    "preprocessor_config.json",
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", type=Path, required=True)
    ap.add_argument("--adapter", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--dtype", default="bfloat16")
    ap.add_argument("--check", action="store_true", help="統合前後で logits が一致するか確かめる")
    args = ap.parse_args()

    from peft import PeftModel
    from transformers import AutoModelForImageTextToText

    dtype = getattr(torch, args.dtype)
    base = AutoModelForImageTextToText.from_pretrained(args.base, dtype=dtype, device_map="cpu")
    model = PeftModel.from_pretrained(base, args.adapter)
    probe = torch.tensor([[2, 105, 2364, 107, 236767, 106, 107, 105, 4368, 107]])
    before = model(input_ids=probe).logits.float() if args.check else None
    merged = model.merge_and_unload()
    if before is not None:
        after = merged(input_ids=probe).logits.float()
        diff = (before - after).abs().max().item()
        print(f"統合前後の logits の最大差 {diff:.2e}")
        if diff > (1e-3 if dtype == torch.float32 else 5e-2):
            raise SystemExit("⚠ 統合前後で出力が一致しない — 書き出さない")
    args.out.mkdir(parents=True, exist_ok=True)
    merged.save_pretrained(args.out, safe_serialization=True)
    for name in _SIDECAR:
        src = args.base / name
        if src.exists():
            shutil.copy2(src, args.out / name)
    print(f"書込: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
