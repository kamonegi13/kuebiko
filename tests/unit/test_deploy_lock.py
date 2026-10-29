"""scripts/lib/deploy_lock.sh のロックと成功の印 (一時ディレクトリで bash を起動して確かめる)。"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[2] / "scripts" / "lib" / "deploy_lock.sh"
MAX_AGE = 7200


def _bash(script: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'. "{LIB}"\n{script}'],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        timeout=60,
        check=False,
    )


def _dead_pid() -> int:
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def _make_lock(lock: Path, pid: int | str | None, started: int | None) -> None:
    lock.mkdir()
    if pid is not None:
        (lock / "pid").write_text(f"{pid}\n")
    if started is not None:
        (lock / "started_epoch").write_text(f"{started}\n")


def _acquire(lock: Path) -> subprocess.CompletedProcess[str]:
    return _bash(f'acquire_lock "{lock}" 0 1')


def test_acquires_when_absent_and_writes_pid_and_epoch(tmp_path: Path) -> None:
    lock = tmp_path / "l"
    r = _bash(f'acquire_lock "{lock}" 0 1 && cat "{lock}/pid" "{lock}/started_epoch"')
    assert r.returncode == 0
    pid, started = r.stdout.split()
    assert pid.isdigit() and abs(int(started) - time.time()) < 30


def test_empty_stale_dir_by_mtime_is_taken(tmp_path: Path) -> None:
    lock = tmp_path / "l"
    lock.mkdir()
    old = time.time() - MAX_AGE - 60
    os.utime(lock, (old, old))
    assert _acquire(lock).returncode == 0
    assert (lock / "pid").exists()


def test_young_empty_dir_is_not_taken(tmp_path: Path) -> None:
    lock = tmp_path / "l"
    lock.mkdir()
    assert _acquire(lock).returncode == 1


def test_dead_pid_lock_is_taken(tmp_path: Path) -> None:
    lock = tmp_path / "l"
    _make_lock(lock, _dead_pid(), int(time.time()))
    assert _acquire(lock).returncode == 0
    assert (lock / "pid").read_text().strip() != ""
    assert not list(tmp_path.glob("l.stale.*"))


def test_live_pid_lock_is_kept(tmp_path: Path) -> None:
    lock = tmp_path / "l"
    _make_lock(lock, os.getpid(), int(time.time()))
    assert _acquire(lock).returncode == 1
    assert (lock / "pid").read_text().strip() == str(os.getpid())


def test_live_pid_but_older_than_two_hours_is_taken(tmp_path: Path) -> None:
    lock = tmp_path / "l"
    _make_lock(lock, os.getpid(), int(time.time()) - MAX_AGE - 10)
    assert _acquire(lock).returncode == 0


def test_garbage_pid_is_treated_as_stale(tmp_path: Path) -> None:
    lock = tmp_path / "l"
    _make_lock(lock, "not-a-pid", int(time.time()))
    assert _acquire(lock).returncode == 0


@pytest.mark.parametrize("round_no", range(3))
def test_two_simultaneous_takers_of_a_stale_lock_yield_one_winner(
    tmp_path: Path, round_no: int
) -> None:
    lock = tmp_path / f"l{round_no}"
    _make_lock(lock, _dead_pid(), int(time.time()))
    # 勝者は取った後も生きている (終了した pid を後から「古い」と奪われないよう保持する)
    cmd = ["bash", "-c", f'. "{LIB}"; acquire_lock "{lock}" 0 1 && sleep 3']
    procs = [subprocess.Popen(cmd) for _ in range(2)]
    codes = sorted(p.wait(timeout=60) for p in procs)
    assert codes == [0, 1]


def test_release_removes_only_own_lock(tmp_path: Path) -> None:
    lock = tmp_path / "l"
    r = _bash(f'acquire_lock "{lock}" 0 1 && release_lock "{lock}"')
    assert r.returncode == 0 and not lock.exists()
    _make_lock(tmp_path / "other", 999999, int(time.time()))
    _bash(f'release_lock "{tmp_path / "other"}"')
    assert (tmp_path / "other").exists()


def test_success_stamp_is_valid_json_and_fresh(tmp_path: Path) -> None:
    stamp = tmp_path / "ok.json"
    r = _bash(f'write_success_stamp "{stamp}" public abc123 unchanged 4.147.0')
    assert r.returncode == 0
    data = json.loads(stamp.read_text())
    assert data["side"] == "public" and data["result"] == "unchanged"
    assert data["digest"] == "abc123" and data["wrangler"] == "4.147.0"
    assert abs(data["ts_epoch"] - time.time()) < 30
    assert not list(tmp_path.glob("ok.json.tmp.*"))
    age = _bash(f'stamp_age_seconds "{stamp}"')
    assert age.returncode == 0 and int(age.stdout) < 30


def test_stamp_age_fails_for_missing_or_broken_file(tmp_path: Path) -> None:
    assert _bash(f'stamp_age_seconds "{tmp_path / "none.json"}"').returncode == 1
    bad = tmp_path / "bad.json"
    bad.write_text("{oops")
    assert _bash(f'stamp_age_seconds "{bad}"').returncode == 1
