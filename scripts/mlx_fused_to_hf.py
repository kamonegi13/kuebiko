#!/usr/bin/env python3
"""MLX で fuse した Gemma 4 MoE の重みを HF 規約のテンソル名へ戻す。

`mlx_lm.fuse` は MLX 内部の名前 (``experts.switch_glu.{gate,up,down}_proj.weight``)
で書き出すため、HF の名前を前提とする llama.cpp の ``convert_hf_to_gguf.py`` が
expert 層を認識できず GGUF 化できない。ここでは mlx-lm の ``sanitize()`` の逆変換を
行い、公式 checkpoint と同じ ``experts.{gate_up_proj,down_proj}`` へ戻す。

逆変換の根拠 (mlx_lm/models/gemma4_text.py:629-639):
    gate, up = mx.split(v, 2, axis=-2)   # gate が前半・up が後半
    → よって gate_up_proj = concatenate([gate, up], axis=-2)
    down_proj は名前だけ変わり中身は不変。

接頭辞の語順差 (``language_model.model.`` → ``model.language_model.``) も戻す。
llama.cpp の Python 変換器は ``language_model.`` を無条件に除去するのでどちらでも通るが、
**Ollama の Go 変換器は接頭辞を前方一致で剥がす**ため、語順が違うと層を特定できず
``Failed to determine layer for tensor language_model.model.layers.0.ffn_down.weight``
で落ちる。公式 checkpoint と同じ語順に揃えれば両方の経路で通る。

使用例:
    data/mlx/venv/bin/python scripts/mlx_fused_to_hf.py \
        --src data/mlx/fused-bf16 --dst data/mlx/hf-remap
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import mlx.core as mx

# MLX が expert を格納する中間ノード名。HF にはこの階層が無い。
_SWITCH_GLU = ".switch_glu"

# 接頭辞の語順。MLX は ``language_model.model.``、HF 公式は ``model.language_model.``。
_MLX_PREFIX = "language_model.model."
_HF_PREFIX = "model.language_model."

# 出力 shard の目安サイズ。入力と同程度に保ち、1 shard を丸ごとメモリに載せられる幅にする。
_SHARD_BYTES = 5 * 1000**3

# 重みと一緒に持ち出す付随ファイル (tokenizer / chat template 等)。
_SIDECAR_FILES = (
    "config.json",
    "generation_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "chat_template.jinja",
    "special_tokens_map.json",
    "tokenizer.model",
)


class RemapError(RuntimeError):
    """入力が想定した MLX fuse 出力の形をしていない。"""


def _retitle(mlx_name: str) -> str:
    """接頭辞を HF 公式の語順へ戻す。"""
    if mlx_name.startswith(_MLX_PREFIX):
        return _HF_PREFIX + mlx_name[len(_MLX_PREFIX) :]
    return mlx_name


def _hf_name(mlx_name: str) -> str | None:
    """MLX の名前を HF の名前へ写す。gate/up は結合が要るので None を返す。"""
    if f"{_SWITCH_GLU}.gate_proj.weight" in mlx_name or f"{_SWITCH_GLU}.up_proj.weight" in mlx_name:
        return None
    name = _retitle(mlx_name)
    if f"{_SWITCH_GLU}.down_proj.weight" in name:
        return name.replace(f"{_SWITCH_GLU}.down_proj.weight", ".down_proj")
    return name


def _expert_base(mlx_name: str) -> str:
    """``...layers.0.experts.switch_glu.gate_proj.weight`` → ``...layers.0.experts`` (HF 語順)"""
    name = _retitle(mlx_name)
    return name[: name.index(_SWITCH_GLU)]


def _fuse_gate_up(gate: mx.array, up: mx.array) -> mx.array:
    """mlx-lm の ``mx.split(v, 2, axis=-2)`` の逆。gate が前半・up が後半。"""
    if gate.shape != up.shape:
        raise RemapError(f"gate と up の形が違う: {gate.shape} vs {up.shape}")
    return mx.concatenate([gate, up], axis=-2)


class ShardWriter:
    """テンソルを受け取り、目安サイズごとに safetensors shard へ書き出す。"""

    def __init__(self, dst: Path, shard_bytes: int = _SHARD_BYTES) -> None:
        self._dst = dst
        self._shard_bytes = shard_bytes
        self._buffer: dict[str, mx.array] = {}
        self._buffered_bytes = 0
        self._weight_map: dict[str, str] = {}
        self._total_bytes = 0
        self._shards: list[dict[str, mx.array]] = []

    def add(self, name: str, tensor: mx.array) -> None:
        self._buffer[name] = tensor
        self._buffered_bytes += tensor.nbytes
        self._total_bytes += tensor.nbytes
        if self._buffered_bytes >= self._shard_bytes:
            self._flush()

    def _flush(self) -> None:
        if not self._buffer:
            return
        self._shards.append(self._buffer)
        self._buffer = {}
        self._buffered_bytes = 0

    def finalize(self) -> tuple[int, int]:
        """shard を確定し、実ファイルと index.json を書き出す。"""
        self._flush()
        total = len(self._shards)
        for i, shard in enumerate(self._shards, start=1):
            filename = f"model-{i:05d}-of-{total:05d}.safetensors"
            path = self._dst / filename
            print(f"  write {filename} ({len(shard)} tensors)", flush=True)
            mx.save_safetensors(str(path), shard)
            for name in shard:
                self._weight_map[name] = filename
            shard.clear()
        index = {
            "metadata": {"total_size": self._total_bytes},
            "weight_map": self._weight_map,
        }
        (self._dst / "model.safetensors.index.json").write_text(
            json.dumps(index, indent=2) + "\n", encoding="utf-8"
        )
        return total, len(self._weight_map)


def _shard_order(src: Path) -> list[Path]:
    index_path = src / "model.safetensors.index.json"
    if not index_path.is_file():
        raise RemapError(f"index が無い: {index_path}")
    weight_map: dict[str, str] = json.loads(index_path.read_text(encoding="utf-8"))["weight_map"]
    return [src / name for name in sorted(set(weight_map.values()))]


def remap(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    writer = ShardWriter(dst)
    # 相方待ちの gate / up。同一 shard に収まる保証がないので跨いで持ち越す。
    pending: dict[str, dict[str, mx.array]] = {}
    seen_experts = 0

    for shard_path in _shard_order(src):
        print(f"read {shard_path.name}", flush=True)
        weights = mx.load(str(shard_path))
        for name in weights:
            tensor = weights[name]
            hf_name = _hf_name(name)
            if hf_name is not None:
                writer.add(hf_name, tensor)
                continue
            base = _expert_base(name)
            half = "gate" if f"{_SWITCH_GLU}.gate_proj.weight" in name else "up"
            slot = pending.setdefault(base, {})
            slot[half] = tensor
            if "gate" in slot and "up" in slot:
                writer.add(f"{base}.gate_up_proj", _fuse_gate_up(slot["gate"], slot["up"]))
                del pending[base]
                seen_experts += 1

    if pending:
        raise RemapError(f"相方の見つからない expert が残った: {sorted(pending)[:3]}")

    for filename in _SIDECAR_FILES:
        source = src / filename
        if source.is_file():
            shutil.copy2(source, dst / filename)

    shards, tensors = writer.finalize()
    print(f"done: {tensors} tensors / {shards} shards / experts fused for {seen_experts} layers")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, required=True, help="mlx_lm.fuse の出力ディレクトリ")
    parser.add_argument("--dst", type=Path, required=True, help="HF 形式の書き出し先")
    args = parser.parse_args()
    try:
        remap(args.src, args.dst)
    except RemapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
