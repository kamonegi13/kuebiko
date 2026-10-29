"""scripts/lib/deploy_lock.sh のロック・時間の上限・成功の印 (/bin/bash で確かめる)。"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[2] / "scripts" / "lib" / "deploy_lock.sh"
MAX_AGE = 7200
BASH = "/bin/bash"  # macOS の bash 3.2 (LaunchAgent が使うもの)
# 配信の本体とみなす印。本番の deploy_site.sh と取り違えないようテスト固有にする
MARK = f"lockmark_{uuid.uuid4().hex[:10]}"


def _bash(script: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, "-c", f'. "{LIB}"\n{script}', MARK],
        capture_output=True,
        text=True,
        env={**os.environ, "DEPLOY_LOCK_OWNER_PATTERN": MARK, **env},
        timeout=90,
        check=False,
    )


def _dead_pid() -> int:
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


@pytest.fixture
def owner() -> Iterator[subprocess.Popen[bytes]]:
    """配信の本体のふりをして生きているプロセス (command に MARK を含む)。"""
    p = subprocess.Popen([BASH, "-c", "sleep 60; :", MARK])
    time.sleep(0.2)
    yield p
    p.kill()
    p.wait()


@pytest.fixture
def stranger() -> Iterator[subprocess.Popen[bytes]]:
    """生きているが配信の本体ではないプロセス (pid の再利用の想定)。"""
    p = subprocess.Popen(["sleep", "60"])
    yield p
    p.kill()
    p.wait()


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


def test_empty_old_dir_without_other_owner_is_taken(tmp_path: Path) -> None:
    lock = tmp_path / "l"
    lock.mkdir()
    old = time.time() - MAX_AGE - 60
    os.utime(lock, (old, old))
    assert _acquire(lock).returncode == 0
    assert (lock / "pid").exists()


def test_empty_old_dir_with_other_owner_running_is_kept(
    tmp_path: Path, owner: subprocess.Popen[bytes]
) -> None:
    lock = tmp_path / "l"
    lock.mkdir()
    old = time.time() - MAX_AGE - 60
    os.utime(lock, (old, old))
    assert _acquire(lock).returncode == 1
    assert not (lock / "pid").exists()


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


def test_live_owner_lock_is_kept(tmp_path: Path, owner: subprocess.Popen[bytes]) -> None:
    lock = tmp_path / "l"
    _make_lock(lock, owner.pid, int(time.time()))
    assert _acquire(lock).returncode == 1
    assert (lock / "pid").read_text().strip() == str(owner.pid)


def test_live_owner_older_than_two_hours_is_still_kept(
    tmp_path: Path, owner: subprocess.Popen[bytes]
) -> None:
    lock = tmp_path / "l"
    _make_lock(lock, owner.pid, int(time.time()) - MAX_AGE - 10)
    assert _acquire(lock).returncode == 1
    assert (lock / "pid").read_text().strip() == str(owner.pid)


def test_live_pid_that_is_not_the_deploy_script_is_taken(
    tmp_path: Path, stranger: subprocess.Popen[bytes]
) -> None:
    lock = tmp_path / "l"
    _make_lock(lock, stranger.pid, int(time.time()))
    assert _acquire(lock).returncode == 0


def test_garbage_pid_is_treated_as_stale(tmp_path: Path) -> None:
    lock = tmp_path / "l"
    _make_lock(lock, "not-a-pid", int(time.time()))
    assert _acquire(lock).returncode == 0


def test_take_stale_restores_lock_when_pid_changed_since_judgement(tmp_path: Path) -> None:
    lock = tmp_path / "l"
    _make_lock(lock, 222, int(time.time()))
    r = _bash(f'_dl_take_stale "{lock}" 111')
    assert r.returncode == 1
    assert (lock / "pid").read_text().strip() == "222"
    assert not list(tmp_path.glob("l.stale.*"))


@pytest.mark.parametrize("round_no", range(3))
def test_two_simultaneous_takers_of_a_stale_lock_yield_one_winner(
    tmp_path: Path, round_no: int
) -> None:
    lock = tmp_path / f"l{round_no}"
    _make_lock(lock, _dead_pid(), int(time.time()))
    # 勝者は取った後も生きている (終了した pid を後から「古い」と奪われないよう保持する)
    cmd = [BASH, "-c", f'. "{LIB}"; acquire_lock "{lock}" 0 1 && sleep 3', MARK]
    env = {**os.environ, "DEPLOY_LOCK_OWNER_PATTERN": MARK}
    procs = [subprocess.Popen(cmd, env=env) for _ in range(2)]
    codes = sorted(p.wait(timeout=60) for p in procs)
    assert codes == [0, 1]


def test_release_removes_only_own_lock(tmp_path: Path) -> None:
    lock = tmp_path / "l"
    r = _bash(f'acquire_lock "{lock}" 0 1 && release_lock "{lock}"')
    assert r.returncode == 0 and not lock.exists()
    _make_lock(tmp_path / "other", 999999, int(time.time()))
    _bash(f'release_lock "{tmp_path / "other"}"')
    assert (tmp_path / "other").exists()


def test_run_with_timeout_passes_through_success_and_failure() -> None:
    ok = _bash("run_with_timeout 10 'Deployment complete' echo hello")
    assert ok.returncode == 0 and "hello" in ok.stdout
    bad = _bash("run_with_timeout 10 'Deployment complete' bash -c 'echo boom; exit 3'")
    assert bad.returncode == 3 and "boom" in bad.stdout


def test_run_with_timeout_kills_hung_command_and_returns_124() -> None:
    start = time.time()
    r = _bash("run_with_timeout 2 'Deployment complete' bash -c 'sleep 60'")
    assert r.returncode == 124
    assert time.time() - start < 20


def test_run_with_timeout_treats_complete_line_then_hang_as_success() -> None:
    start = time.time()
    r = _bash(
        "run_with_timeout 30 'Deployment complete' bash -c 'echo Deployment complete; sleep 60'",
        DEPLOY_COMPLETE_GRACE="1",
    )
    assert r.returncode == 0 and "Deployment complete" in r.stdout
    assert time.time() - start < 20


def test_run_with_timeout_complete_line_at_the_limit_is_success() -> None:
    r = _bash(
        "run_with_timeout 2 'Deployment complete' bash -c 'echo Deployment complete; sleep 60'",
        DEPLOY_COMPLETE_GRACE="100",
    )
    assert r.returncode == 0


def test_run_with_timeout_kills_grandchildren() -> None:
    _bash("run_with_timeout 2 'never' bash -c 'sleep 61 & sleep 62; wait'")
    time.sleep(0.5)
    left = subprocess.run(["pgrep", "-f", "sleep 6[12]$"], capture_output=True, text=True)
    assert left.stdout.strip() == ""


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
