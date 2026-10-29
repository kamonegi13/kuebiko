"""replay の manifest。記事本文は書かない (id と数字のみ)。"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

MAX_STRING_LEN = 300
FORBIDDEN_KEYS = frozenset(
    {"body", "body_ja", "text", "content", "linked_context", "prompt_text", "summary", "raw"}
)


class BodyLeakError(ValueError):
    """manifest / 出力に本文らしきものが入っている。"""


def assert_no_body(value: Any, path: str = "$") -> None:
    """禁止キーと長い文字列 (本文の疑い) を再帰的に拒否する。"""
    if isinstance(value, dict):
        for k, v in value.items():
            if str(k) in FORBIDDEN_KEYS:
                raise BodyLeakError(f"forbidden key at {path}.{k}")
            assert_no_body(v, f"{path}.{k}")
    elif isinstance(value, list | tuple):
        for i, v in enumerate(value):
            assert_no_body(v, f"{path}[{i}]")
    elif isinstance(value, str) and len(value) > MAX_STRING_LEN:
        raise BodyLeakError(f"string too long at {path} ({len(value)} chars)")


@dataclass(frozen=True)
class Manifest:
    name: str
    period_start: str
    period_end: str
    split_cutoff: str
    input_kinds: dict[str, str]  # 入力 -> point-in-time の種別
    head_sha: str
    prereg_path: str
    prereg_git_sha: str
    prereg_blob_sha256: str
    judge_sets: list[dict[str, Any]]
    model_tiers_version: int | None
    r18_version: str
    flags: dict[str, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)


def write_manifest(manifest: Manifest, out_dir: Path) -> Path:
    """out_dir/manifest.json へ atomic に書く。"""
    payload = asdict(manifest)
    assert_no_body(payload)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "manifest.json"
    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=".manifest-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target
