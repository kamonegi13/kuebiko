"""prereg の関門と判定集の同一性 (path + sha256 + id)。"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

PREREG_DIR = Path("docs/replay_prereg")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]*$")
_READ_CHUNK = 1 << 20
_ID_KEYS = ("article_id", "event_id", "item_id", "url_hash", "id")


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
    commit_time: str  # prereg の最終コミットの時刻 (ISO)
    run_started_at: str  # 実行開始時刻 (ISO、prereg のコミットより厳密に後)


def head_sha(repo_root: Path) -> str:
    res = _git(repo_root, "rev-parse", "HEAD")
    if res.returncode != 0:
        raise GateRefusedError(f"cannot resolve HEAD: {res.stderr.strip()}")
    return res.stdout.strip()


def _assert_clean(repo_root: Path, rel: str, guarded: list[str]) -> None:
    status = _git(repo_root, "status", "--porcelain", "--", rel, *guarded)
    if status.stdout.strip():
        raise GateRefusedError(f"uncommitted changes in guarded paths: {status.stdout.strip()}")
    flags = _git(repo_root, "ls-files", "-v", "--", rel, *guarded).stdout.splitlines()
    hidden = [line for line in flags if line and line[0] != "H"]
    if hidden:
        raise GateRefusedError(f"skip-worktree/assume-unchanged files: {hidden}")


def _assert_outputs_not_older(out_dir: Path, commit_time: datetime) -> None:
    if not out_dir.is_dir():
        return
    stamp = commit_time.timestamp()
    old = [f.name for f in out_dir.iterdir() if f.is_file() and f.stat().st_mtime < stamp]
    if old:
        raise GateRefusedError(f"output dir has results older than the prereg commit: {old[:5]}")


def require_prereg(
    name: str,
    repo_root: Path,
    *,
    run_started_at: datetime | None = None,
    caller: Path | None = None,
    out_dir: Path | None = None,
) -> PreregRecord:
    """prereg が存在し、コミット済みで、実行開始より厳密に前のコミットであるとき記録を返す。

    scripts/replay と呼び出しスクリプトも未コミット変更・skip-worktree があれば拒否する。
    """
    if not _NAME_RE.match(name) or name.upper() == "README":
        raise GateRefusedError(f"invalid prereg name: {name!r}")
    rel = (PREREG_DIR / f"{name}.md").as_posix()
    full = repo_root / rel
    if not full.is_file():
        raise GateRefusedError(f"prereg missing: {rel}")
    if _git(repo_root, "ls-files", "--error-unmatch", "--", rel).returncode != 0:
        raise GateRefusedError(f"prereg is not tracked by git: {rel}")
    guarded = ["scripts/replay"]
    if caller is not None:
        guarded.append(caller.resolve().relative_to(repo_root.resolve()).as_posix())
    _assert_clean(repo_root, rel, guarded)
    sha = _git(repo_root, "log", "-1", "--format=%H", "--", rel).stdout.strip()
    if not sha:
        raise GateRefusedError(f"prereg has no commit: {rel}")
    head = head_sha(repo_root)
    if _git(repo_root, "merge-base", "--is-ancestor", sha, head).returncode != 0:
        raise GateRefusedError(f"prereg commit is not an ancestor of HEAD: {rel}")
    commit_time = datetime.fromisoformat(
        _git(repo_root, "log", "-1", "--format=%cI", sha).stdout.strip()
    )
    started = run_started_at or datetime.now(UTC)
    if started.tzinfo is None:
        raise GateRefusedError("run_started_at must be timezone-aware")
    if not commit_time < started:
        raise GateRefusedError("prereg commit is not strictly before the run start")
    if out_dir is not None:
        _assert_outputs_not_older(out_dir, commit_time)
    return PreregRecord(
        rel, sha, sha256_file(full), head, commit_time.isoformat(), started.isoformat()
    )


@dataclass(frozen=True)
class TaintedRegistry:
    paths: frozenset[str]
    sha256s: frozenset[str]
    ids: frozenset[str] = frozenset()  # "key:value" 形式
    missing: tuple[str, ...] = ()  # 台帳にあるが手元に無い tainted ファイル


def _expand(repo_root: Path, rel: str) -> list[Path]:
    p = repo_root / rel
    if p.is_dir():
        return sorted(f for f in p.rglob("*") if f.is_file())
    return [p] if p.is_file() else []


def read_ids(path: Path) -> set[str]:
    """jsonl / json(list) の各レコードから id 系の値を "key:value" で集める。読めない行は飛ばす。"""
    ids: set[str] = set()
    text = path.read_text(encoding="utf-8", errors="replace")
    records: list[object] = []
    if path.suffix == ".json":
        try:
            loaded = json.loads(text)
        except ValueError:
            return ids
        records = loaded if isinstance(loaded, list) else [loaded]
    elif path.suffix == ".jsonl":
        for line in text.splitlines():
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
    for rec in records:
        if isinstance(rec, dict):
            ids.update(f"{k}:{rec[k]}" for k in _ID_KEYS if isinstance(rec.get(k), str | int))
    return ids


def load_tainted_registry(ledger_path: Path, repo_root: Path) -> TaintedRegistry:
    """台帳 gold_sets の tainted: true の files から path・sha256・id の集合を作る。

    手元に無いファイルは黙って通さず warnings.warn し、registry.missing に残す。
    """
    data: Any = yaml.safe_load(ledger_path.read_text(encoding="utf-8"))
    paths: set[str] = set()
    shas: set[str] = set()
    ids: set[str] = set()
    missing: list[str] = []
    for gs in data.get("gold_sets", []) or []:
        if not gs.get("tainted"):
            continue
        files = list(gs.get("files") or [])
        if isinstance(gs.get("file"), str) and gs["file"] != "unknown":
            files.append(gs["file"])
        for rel in files:
            paths.add(Path(rel).as_posix())
            found = _expand(repo_root, rel)
            if not found:
                missing.append(Path(rel).as_posix())
            for f in found:
                paths.add(f.relative_to(repo_root).as_posix())
                shas.add(sha256_file(f))
                ids |= read_ids(f)
    if missing:
        warnings.warn(f"tainted files not found locally: {missing}", stacklevel=2)
    return TaintedRegistry(frozenset(paths), frozenset(shas), frozenset(ids), tuple(missing))


@dataclass(frozen=True)
class JudgeSet:
    path: str
    sha256: str
    rows: int


def identify_judge_set(path: Path, registry: TaintedRegistry, repo_root: Path) -> JudgeSet:
    """判定集を path + sha256 で同定する。tainted と path・sha・id が重なれば拒否。"""
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
    overlap = read_ids(path) & registry.ids
    if overlap:
        raise GateRefusedError(f"judge set shares {len(overlap)} ids with tainted sets: {rel}")
    with open(path, "rb") as fh:
        rows = sum(1 for line in fh if line.strip())
    return JudgeSet(rel, sha, rows)
