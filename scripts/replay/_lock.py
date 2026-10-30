"""裁定の台帳のファイルロック。2 本の replay が同じ項目を同時に裁定しない。"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

DEFAULT_LOCK_PATH = Path("data/eval/adjudication.lock")


class AdjudicationLockBusyError(RuntimeError):
    """別の replay が裁定の台帳を使用中。"""


@contextmanager
def adjudication_lock(path: Path = DEFAULT_LOCK_PATH, *, owner: str = "") -> Iterator[Path]:
    """排他ロック (待たずに失敗する)。owner は診断用に書き込む (本文は書かない)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")  # noqa: SIM115
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AdjudicationLockBusyError(f"adjudication ledger is locked: {path}") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} owner={owner}\n")
        handle.flush()
        try:
            yield path
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
