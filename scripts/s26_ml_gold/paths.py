"""S26-ML-GOLD の出力の置き場 (worktree では本体の checkout の data/)。"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def find_data_root() -> Path:
    """data/eval を持つ最初の親。KUEBIKO_DATA_ROOT で上書き。"""
    env = os.environ.get("KUEBIKO_DATA_ROOT")
    if env:
        return Path(env)
    for p in (ROOT, *ROOT.parents):
        if (p / "data" / "eval").is_dir():
            return p
    raise RuntimeError("data/eval not found")


DATA_ROOT = find_data_root()
OUT = DATA_ROOT / "data" / "eval" / "s26_ml_gold"


def out_dir(name: str, window_no: int | None = None) -> Path:
    d = OUT / name
    return d / f"w{window_no}" if window_no is not None else d
