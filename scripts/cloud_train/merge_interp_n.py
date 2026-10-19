#!/usr/bin/env python3
"""N 本の PEFT アダプタを ΔW = Σ w_i·ΔW_i (Σw_i=1) で混ぜて統合する (補間試験、2026-10-09)。

A と B を別々に平均すると交差項が出るため、層ごとに ΔW を作ってから混ぜる。
統合の丸め方は merge_adapter.py と同じ (float32 加算 + 確率的丸め)。
"""

from __future__ import annotations

import argparse
import gc
import shutil
import sys
from pathlib import Path

import torch
from safetensors import safe_open
from safetensors.torch import save_file

sys.path.insert(0, str(Path(__file__).parent))
import merge_adapter as ma  # noqa: E402


def interp(base: Path, adapters: list[Path], weights: list[float], out: Path) -> int:
    loaded = [ma._deltas(a) for a in adapters]
    shards = sorted(base.glob("*.safetensors"))
    shapes: dict[str, torch.Size] = {}
    for sh in shards:
        with safe_open(str(sh), "pt") as f:
            for k in f.keys():  # noqa: SIM118
                shapes[k] = torch.Size(f.get_slice(k).get_shape())
    names = set(shapes)
    tg = [ma._targets(p, names, shapes) for p, _ in loaded]
    if any(set(tg[0]) != set(x) for x in tg[1:]):
        raise SystemExit("アダプタの対象が一致しない")
    scales = [s for _, s in loaded]
    out.mkdir(parents=True, exist_ok=True)
    merged = 0
    for sh in shards:
        tensors: dict[str, torch.Tensor] = {}
        with safe_open(str(sh), "pt") as f:
            meta = f.metadata()
            for k in f.keys():  # noqa: SIM118
                w = f.get_tensor(k)
                if k in tg[0]:
                    acc = w.float()
                    for i, weight in enumerate(weights):
                        if weight == 0.0:
                            continue
                        for ab in tg[i][k]:
                            d = (
                                ma._linear_delta(ab, scales[i])
                                if w.dim() == 2
                                else ma._experts_delta(ab, scales[i], w.shape)
                            )
                            acc += weight * d
                    w = ma.stochastic_round_bf16(acc, seed=merged + 1)
                    merged += 1
                tensors[k] = w
        save_file(tensors, str(out / sh.name), metadata=meta)
        del tensors
        gc.collect()
    for p in base.iterdir():
        if p.suffix != ".safetensors" and p.is_file():
            shutil.copy2(p, out / p.name)
    return merged


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", type=Path, required=True)
    ap.add_argument(
        "--adapter", action="append", required=True, help="<dir>:<weight> (複数、重みの和は 1)"
    )
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    pairs = [x.rsplit(":", 1) for x in args.adapter]
    paths = [Path(p) for p, _ in pairs]
    weights = [float(w) for _, w in pairs]
    if abs(sum(weights) - 1.0) > 1e-6:
        raise SystemExit("重みの和が 1 でない")
    n = interp(args.base, paths, weights, args.out)
    print(f"統合した重み {n} 本 (weights={weights}) → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
