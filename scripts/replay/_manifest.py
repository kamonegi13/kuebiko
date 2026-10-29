"""replay の manifest。記事本文は書かない (id と数字のみ)。"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

MAX_ID_LEN = 64
MAX_PATH_LEN = 200
# 本文は空白・日本語・改行を含む。id / 時刻 / 列挙値 / パスの文字だけを許す (allowlist)。
_SAFE_STR_RE = re.compile(r"^[A-Za-z0-9_.:/+\-=#@]*$")
FORBIDDEN_KEYS = frozenset(
    {"body", "body_ja", "text", "content", "linked_context", "prompt_text", "summary", "raw",
     "title", "excerpt", "snippet", "claim"}
)  # fmt: skip


class BodyLeakError(ValueError):
    """manifest / 出力に本文らしきものが入っている。"""


def _check_str(value: str, key: str, where: str) -> None:
    limit = MAX_PATH_LEN if key.endswith("path") else MAX_ID_LEN
    if len(value) > limit or not _SAFE_STR_RE.match(value):
        raise BodyLeakError(f"string is not an id/timestamp/enum/path at {where}")


def assert_no_body(value: Any, path: str = "$", key: str = "") -> None:
    """型の allowlist (数値・bool・None・安全文字の短い文字列) 以外を再帰的に拒否する。"""
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str) or k.lower() in FORBIDDEN_KEYS:
                raise BodyLeakError(f"forbidden key at {path}.{k}")
            _check_str(k, "", f"{path}.<key>")
            assert_no_body(v, f"{path}.{k}", k)
    elif isinstance(value, list | tuple):
        for i, v in enumerate(value):
            assert_no_body(v, f"{path}[{i}]", key)
    elif isinstance(value, bool) or value is None or isinstance(value, int):
        return
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise BodyLeakError(f"non-finite number at {path}")
    elif isinstance(value, str):
        _check_str(value, key, path)
    else:
        raise BodyLeakError(f"disallowed type {type(value).__name__} at {path}")


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
