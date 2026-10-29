#!/usr/bin/env python3
"""GPU の枠の前に本番の毎時のジョブを止め、trap で必ず戻す (GPU-TOOLS-PSTOP)。

枠の開始 T の 40 分前に、いま enabled のものだけを止め、止めたものを印のファイルに書く。
戻しは**自分が止めたものだけ**を逆順に (watchdog を最初に)。EXIT/INT/TERM/HUP で戻し、
壁時計の hard stop を過ぎても戻す。戻しに失敗したら手動の戻しコマンドを表示する。

    python scripts/gpu_window/pstop.py --dry-run
    python scripts/gpu_window/pstop.py --slot-start 12:00 --hard-stop 15:00
    python scripts/gpu_window/pstop.py --restore-from-marker   # 異常終了の後始末
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import urllib.error
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gpu_window_common import (  # noqa: E402
    DEFAULT_API,
    JST,
    api_busy_jobs,
    http_json,
    next_clock,
)

#: 止める順 (watchdog を最後に止める = 止めたジョブを watchdog が回復しない)。戻しは逆順。
STOP_ORDER = (
    "hourly-collect",
    "hourly-upkeep",
    "ransomware-live-ingest",
    "grok-briefing",
    "job-recovery-watchdog",
)
LEAD_MINUTES = 40
MARKER_PATH = Path("data/eval/plan_ledger/pstop_state.json")
IDLE_POLL_S = 30
RESTORE_RETRIES = 3
EXIT_OK, EXIT_ERROR, EXIT_BUSY, EXIT_STALE_MARKER = 0, 2, 3, 4


@dataclass(frozen=True)
class PlanItem:
    job_id: str
    action: str  # stop | skip
    reason: str
    critical: bool = False


def build_plan(jobs: Sequence[dict[str, Any]], order: Sequence[str] = STOP_ORDER) -> list[PlanItem]:
    """GET /api/v1/jobs の view から計画を作る (副作用なし)。"""
    by_id = {str(j.get("id")): j for j in jobs}
    plan: list[PlanItem] = []
    for job_id in order:
        job = by_id.get(job_id)
        if job is None:
            plan.append(PlanItem(job_id, "skip", "ジョブが存在しない"))
        elif job.get("chain_id"):
            plan.append(
                PlanItem(job_id, "skip", f"チェーン {job['chain_id']} の段 (チェーンで止まる)")
            )
        elif not job.get("enabled"):
            plan.append(PlanItem(job_id, "skip", "元から無効 (触らない)"))
        else:
            plan.append(
                PlanItem(job_id, "stop", "enabled", critical=job.get("protection") == "critical")
            )
    return plan


def restore_order(stopped: Sequence[str]) -> list[str]:
    return list(reversed(stopped))


def manual_restore_commands(to_restore: Sequence[str], api: str) -> list[str]:
    return [
        f"curl -s -X POST {api}/api/v1/jobs/{j}/toggle -H 'Content-Type: application/json'"
        " -d '{\"enabled\": true}'"
        for j in to_restore  # 戻す順のまま (restore_all の失敗リストは既に戻す順)
    ]


def write_marker(path: Path, stopped: Sequence[str], hard_stop: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "stopped": list(stopped),
        "hard_stop": hard_stop.isoformat(),
        "written_at": datetime.now(JST).isoformat(),
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)  # 原子的に差し替える (途中で死んでも半端な印を残さない)


def read_marker(path: Path) -> list[str]:
    return [str(x) for x in json.loads(path.read_text(encoding="utf-8")).get("stopped", [])]


Toggle = Callable[[str, bool, bool], None]


def make_toggle(api: str) -> Toggle:
    def toggle(job_id: str, enabled: bool, confirm: bool) -> None:
        body: dict[str, Any] = {"enabled": enabled}
        if confirm:
            body["confirm"] = True
        http_json("POST", f"{api}/api/v1/jobs/{job_id}/toggle", body)

    return toggle


def restore_all(
    stopped: Sequence[str],
    toggle: Toggle,
    sleep: Callable[[float], None] = time.sleep,
    retries: int = RESTORE_RETRIES,
) -> list[str]:
    """止めたものを逆順に戻す。戻せなかった id を返す (空なら成功)。"""
    failed: list[str] = []
    for job_id in restore_order(stopped):
        for attempt in range(retries):
            try:
                toggle(job_id, True, False)
                break
            except Exception as e:  # noqa: BLE001
                print(f"[pstop] 戻し失敗 {job_id} ({attempt + 1}/{retries}): {e}", file=sys.stderr)
                if attempt + 1 == retries:
                    failed.append(job_id)
                else:
                    sleep(2.0)
    return failed


def print_plan(plan: Sequence[PlanItem]) -> None:
    for p in plan:
        mark = " (critical: confirm つき)" if p.critical else ""
        print(f"  {p.action:5s} {p.job_id}{mark} - {p.reason}")


def _fetch_jobs(api: str) -> list[dict[str, Any]]:
    return list(http_json("GET", f"{api}/api/v1/jobs")["jobs"])


def _stop_all(
    plan: Sequence[PlanItem], toggle: Toggle, stopped: list[str], on_stopped: Callable[[], None]
) -> None:
    """停止の**意図**を先に印へ書いてから toggle する。

    toggle が成功・timeout・例外のどれでも、試みたジョブは戻しの対象に残す (サーバ側で
    適用済みなのにクライアントが切れた場合の戻し忘れを防ぐ。enabled=true は冪等)。
    """
    for p in plan:
        if p.action != "stop":
            continue
        stopped.append(p.job_id)
        on_stopped()
        toggle(p.job_id, False, p.critical)


def verify_stopped(jobs: Sequence[dict[str, Any]], stopped: Sequence[str]) -> list[str]:
    """停止を試みたのに GET で enabled のままのジョブ id。"""
    by_id = {str(j.get("id")): j for j in jobs}
    return [j for j in stopped if by_id.get(j, {}).get("enabled")]


def validate_times(now: datetime, slot: datetime, hard_stop: datetime) -> str | None:
    """枠の時刻の整合。エラーなら理由を返す。"""
    stop_at = slot - timedelta(minutes=LEAD_MINUTES)
    if slot.date() != hard_stop.date():
        return f"slot {slot:%m-%d %H:%M} と hard stop {hard_stop:%m-%d %H:%M} が同じ日でない"
    if stop_at >= hard_stop or slot >= hard_stop:
        return "停止時刻 (T-40 分) ・枠の開始が hard stop より前でない"
    if now >= hard_stop:
        return "hard stop を過ぎている"
    return None


def _wait_idle(api: str, deadline: datetime) -> bool:
    while True:
        if not api_busy_jobs(_fetch_jobs(api)):
            return True
        if datetime.now(JST) >= deadline:
            return False
        time.sleep(IDLE_POLL_S)


def run(args: argparse.Namespace) -> int:
    api = args.api
    marker = Path(args.marker)
    toggle = make_toggle(api)
    if args.restore_from_marker:
        if not marker.exists():
            print(f"[pstop] 印 {marker} が無い。戻すものなし")
            return EXIT_OK
        failed = restore_all(read_marker(marker), toggle)
        if not failed:
            marker.unlink(missing_ok=True)
        return EXIT_OK if not failed else EXIT_ERROR
    if args.dry_run:
        print(f"[pstop] dry-run: GET {api}/api/v1/jobs のみ (変更なし)")
        print_plan(build_plan(_fetch_jobs(api)))
        return EXIT_OK
    if marker.exists():
        print(
            f"[pstop] 前回の印 {marker} が残っている。--restore-from-marker で戻す", file=sys.stderr
        )
        return EXIT_STALE_MARKER

    now = datetime.now(JST)
    hard_stop = next_clock(now, args.hard_stop)
    slot = next_clock(now, args.slot_start)
    stop_at = slot - timedelta(minutes=LEAD_MINUTES)
    bad = validate_times(now, slot, hard_stop)
    if bad:
        print(f"[pstop] 時刻が不正: {bad}", file=sys.stderr)
        return EXIT_ERROR
    stopped: list[str] = []
    state = {"done": False}

    def cleanup(*_: object) -> None:
        if state["done"]:
            return
        state["done"] = True
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, signal.SIG_IGN)  # 戻しの最中は割り込ませない
        failed = restore_all(stopped, toggle)
        if failed:
            print("[pstop] 戻せていない。手動で:", file=sys.stderr)
            for c in manual_restore_commands(failed, api):
                print("  " + c, file=sys.stderr)
        else:
            marker.unlink(missing_ok=True)

    def on_signal(signum: int, _frame: object) -> None:
        cleanup()
        raise SystemExit(128 + signum)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, on_signal)
    try:
        while datetime.now(JST) < stop_at and datetime.now(JST) < hard_stop:
            time.sleep(min(30.0, max(1.0, (stop_at - datetime.now(JST)).total_seconds())))
        plan = build_plan(_fetch_jobs(api))
        print_plan(plan)
        _stop_all(plan, toggle, stopped, lambda: write_marker(marker, stopped, hard_stop))
        still = verify_stopped(_fetch_jobs(api), stopped)
        if still:
            print(f"[pstop] 停止後も enabled のまま: {still}", file=sys.stderr)
            return EXIT_ERROR
        if not _wait_idle(api, min(hard_stop, slot)):
            print("[pstop] 走っている段が締め切りまでに 0 にならない", file=sys.stderr)
            return EXIT_BUSY
        print(
            f"[pstop] 停止済み {stopped}。hard stop {hard_stop:%H:%M} か release ファイルまで保持"
        )
        release = Path(args.release_file)
        while datetime.now(JST) < hard_stop and not release.exists():
            time.sleep(5)
        return EXIT_OK
    except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
        print(f"[pstop] エラー: {e}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        cleanup()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--api", default=DEFAULT_API)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--slot-start", default="12:00", help="枠の開始 T (JST HH:MM)。停止は T-40 分")
    ap.add_argument("--hard-stop", default="15:00", help="壁時計の上限 (JST HH:MM)")
    ap.add_argument("--marker", default=str(MARKER_PATH))
    ap.add_argument("--release-file", default="data/eval/plan_ledger/pstop_release")
    ap.add_argument("--restore-from-marker", action="store_true")
    return run(ap.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
