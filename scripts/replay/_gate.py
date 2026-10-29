"""prereg の関門と判定集の同一性 (path + sha256)。"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PREREG_DIR = Path("docs/replay_prereg")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]*$")
_READ_CHUNK = 1 << 20


class GateRefusedError(RuntimeError):
    """関門が実行を拒否した。"""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_READ_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 (引数リスト・shell なし)
        ["git", "-C", str(repo_root), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )


@dataclass(frozen=True)
class PreregRecord:
    path: str
    git_sha: str
    blob_sha256: str
    head_sha: str


def head_sha(repo_root: Path) -> str:
    res = _git(repo_root, "rev-parse", "HEAD")
    if res.returncode != 0:
        raise GateRefusedError(f"cannot resolve HEAD: {res.stderr.strip()}")
    return res.stdout.strip()


def require_prereg(name: str, repo_root: Path) -> PreregRecord:
    """docs/replay_prereg/<name>.md が存在し、コミット済みで、HEAD の祖先のとき記録を返す。"""
    if not _NAME_RE.match(name) or name.upper() == "README":
        raise GateRefusedError(f"invalid prereg name: {name!r}")
    rel = (PREREG_DIR / f"{name}.md").as_posix()
    full = repo_root / rel
    if not full.is_file():
        raise GateRefusedError(f"prereg missing: {rel}")
    if _git(repo_root, "ls-files", "--error-unmatch", "--", rel).returncode != 0:
        raise GateRefusedError(f"prereg is not tracked by git: {rel}")
    if _git(repo_root, "diff", "--quiet", "HEAD", "--", rel).returncode != 0:
        raise GateRefusedError(f"prereg has uncommitted changes: {rel}")
    sha = _git(repo_root, "log", "-1", "--format=%H", "--", rel).stdout.strip()
    if not sha:
        raise GateRefusedError(f"prereg has no commit: {rel}")
    head = head_sha(repo_root)
    if _git(repo_root, "merge-base", "--is-ancestor", sha, head).returncode != 0:
        raise GateRefusedError(f"prereg commit is not an ancestor of HEAD: {rel}")
    return PreregRecord(rel, sha, sha256_file(full), head)


@dataclass(frozen=True)
class TaintedRegistry:
    paths: frozenset[str]
    sha256s: frozenset[str]


def _expand(repo_root: Path, rel: str) -> list[Path]:
    p = repo_root / rel
    if p.is_dir():
        return sorted(f for f in p.rglob("*") if f.is_file())
    return [p] if p.is_file() else []


def load_tainted_registry(ledger_path: Path, repo_root: Path) -> TaintedRegistry:
    """台帳 gold_sets の tainted: true の files から path 集合と実在ファイルの sha256 を作る。"""
    data: Any = yaml.safe_load(ledger_path.read_text(encoding="utf-8"))
    paths: set[str] = set()
    shas: set[str] = set()
    for gs in data.get("gold_sets", []) or []:
        if not gs.get("tainted"):
            continue
        files = list(gs.get("files") or [])
        if isinstance(gs.get("file"), str):
            files.append(gs["file"])
        for rel in files:
            paths.add(Path(rel).as_posix())
            for f in _expand(repo_root, rel):
                paths.add(f.relative_to(repo_root).as_posix())
                shas.add(sha256_file(f))
    return TaintedRegistry(frozenset(paths), frozenset(shas))


@dataclass(frozen=True)
class JudgeSet:
    path: str
    sha256: str
    rows: int


def identify_judge_set(path: Path, registry: TaintedRegistry, repo_root: Path) -> JudgeSet:
    """判定集を path + sha256 で同定する。tainted と path か sha が一致すれば拒否。"""
    if not path.is_file():
        raise GateRefusedError(f"judge set missing: {path}")
    resolved = path.resolve()
    try:
        rel = resolved.relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        rel = resolved.as_posix()
    sha = sha256_file(path)
    if rel in registry.paths:
        raise GateRefusedError(f"judge set path is tainted: {rel}")
    if sha in registry.sha256s:
        raise GateRefusedError(f"judge set sha256 matches a tainted set: {rel}")
    with open(path, "rb") as fh:
        rows = sum(1 for line in fh if line.strip())
    return JudgeSet(rel, sha, rows)
