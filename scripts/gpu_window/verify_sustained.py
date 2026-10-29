#!/usr/bin/env python3
"""連続生成の再測定 (GPU-TOOLS-VERIFY / U-V6-VERIFY)。枠の直前に 1 回走らせる。

固定のプロンプトを 7 回連続で生成し、最小 tok/s・電池・AC・蓋を判定して
data/eval/plan_ledger/env_measurements.jsonl に 1 行追記する (失敗でも pass:false で追記)。

終了コード: 0 合格 / 2 実行エラー / 3 busy が締め切りまでに解けない / 4 AC でない・蓋が閉じている /
5 最小 tok/s < 45 / 6 電池が減った。非 0 なら枠を始めない。
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gpu_window_common import DEFAULT_API, JST, api_busy_jobs, http_json, next_clock  # noqa: E402

EXIT_OK, EXIT_ERROR, EXIT_BUSY, EXIT_POWER, EXIT_SLOW, EXIT_BATTERY = 0, 2, 3, 4, 5, 6
MIN_TOK_S = 45.0
MAX_BATTERY_DROP = 0
RUNS = 7
NUM_PREDICT = 1500
MODEL = "kuebiko-sft:s24ci65"
OLLAMA = "http://127.0.0.1:11434"
OUT = Path("data/eval/plan_ledger/env_measurements.jsonl")
PROMPT = (
    "日本の重要インフラ 14 分野それぞれについて、想定されるサイバー脅威・過去の傾向・"
    "防御の要点を詳しく解説してください。"
)
POLL_S = 30


@dataclass(frozen=True)
class Power:
    on_ac: bool
    battery_pct: int | None
    raw: str


def parse_pmset(text: str) -> Power:
    pct = re.search(r"(\d+)%", text)
    return Power(
        on_ac="'AC Power'" in text.splitlines()[0] if text else False,
        battery_pct=int(pct.group(1)) if pct else None,
        raw=text.strip().splitlines()[-1].strip() if text.strip() else "",
    )


def lid_closed(ioreg_text: str) -> bool:
    return bool(re.search(r'"AppleClamshellState"\s*=\s*Yes', ioreg_text))


def battery_drop_is_noise(start: Power, end: Power) -> bool:
    """AC が測定の最初から最後まで続き、満充電付近か充電中なら、%の揺れは減りとみなさない。

    仕様の max_battery_drop=0 は変えない。ここは表示の揺れ (99→98 等) だけを除く。
    """
    if not (start.on_ac and end.on_ac):
        return False
    charging = "charging" in end.raw.lower() and "not charging" not in end.raw.lower()
    return charging or (end.battery_pct or 0) >= 99


def decide(
    *,
    on_ac: bool,
    lid_is_closed: bool,
    tok_s: Sequence[float],
    battery_drop: int,
    min_tok_s: float = MIN_TOK_S,
    max_drop: int = MAX_BATTERY_DROP,
) -> tuple[int, str]:
    """(終了コード, 理由)。優先順: 電源・蓋 (4) > 遅い (5) > 電池 (6)。"""
    if not on_ac:
        return EXIT_POWER, "AC でない"
    if lid_is_closed:
        return EXIT_POWER, "蓋が閉じている (clamshell)"
    if not tok_s:
        return EXIT_ERROR, "生成の実測がない"
    if min(tok_s) < min_tok_s:
        return EXIT_SLOW, f"最小 tok/s {min(tok_s):.1f} < {min_tok_s}"
    if battery_drop > max_drop:
        return EXIT_BATTERY, f"電池が {battery_drop} 減った"
    return EXIT_OK, "pass"


def build_record(
    *,
    ts: str,
    model: str,
    duration_s: int,
    tok_s: Sequence[float],
    power: str,
    battery_start: int | None,
    battery_end: int | None,
    thermal_events: int,
    production: str,
    code: int,
    reason: str,
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "ts": ts,
        "kind": "sustained_generation",
        "model": model,
        "duration_s": duration_s,
        "runs": len(tok_s),
        "tok_per_s_min": round(min(tok_s), 1) if tok_s else None,
        "tok_per_s_median": round(statistics.median(tok_s), 1) if tok_s else None,
        "tok_per_s_max": round(max(tok_s), 1) if tok_s else None,
        "power": power,
        "battery_start": battery_start,
        "battery_end": battery_end,
        "thermal_events_during": thermal_events,
        "production": production,
        "pass": code == EXIT_OK,
        "exit_code": code,
        "reason": reason,
        "script": "scripts/gpu_window/verify_sustained.py",
    }
    return rec


def append_record(path: Path, rec: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _sh(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False).stdout  # noqa: S603


class ThermalParseError(RuntimeError):
    """pmset の出力が想定の形でない (fail closed で exit 2)。"""


_LOG_TS = re.compile(r"^\s*\d{4}-\d{2}-\d{2} ")


def count_emergency(log_text: str) -> int:
    """AC 中に Thermal Emergency Sleep に入った回数。形式が不明なら例外 (fail closed)。

    数えるのは "Entering Sleep state due to 'Thermal Emergency Sleep'" かつ "Using AC" の行だけ。
    "Ignored DarkWake thermal emergency signal" は無視された信号で数えない。
    (pmset -g thermlog は終了しないので使わない。)
    """
    lines = log_text.splitlines()
    if not any(_LOG_TS.match(ln) for ln in lines):
        raise ThermalParseError("pmset -g log の形式を認識できない")
    count = 0
    for ln in lines:
        low = ln.lower()
        if "entering sleep state due to 'thermal emergency sleep'" in low and "using ac" in low:
            count += 1
    return count


def _thermal_count() -> int:
    return count_emergency(_sh(["pmset", "-g", "log"]))


def _generate(model: str, seed: int) -> float:
    d = http_json(
        "POST",
        f"{OLLAMA}/api/generate",
        {
            "model": model,
            "prompt": PROMPT,
            "stream": False,
            "options": {"num_predict": NUM_PREDICT, "temperature": 0.7, "seed": seed},
        },
        timeout=300,
    )
    return float(d["eval_count"]) / (float(d["eval_duration"]) / 1e9)


def _wait_not_busy(api: str, deadline: datetime) -> bool:
    while True:
        if not api_busy_jobs(list(http_json("GET", f"{api}/api/v1/jobs")["jobs"])):
            return True
        if datetime.now(JST) >= deadline:
            return False
        time.sleep(POLL_S)


def run(args: argparse.Namespace) -> int:
    started = datetime.now(JST)
    deadline = next_clock(started, args.deadline)
    out = Path(args.out)
    power0 = parse_pmset(_sh(["pmset", "-g", "batt"]))
    closed = lid_closed(_sh(["ioreg", "-r", "-k", "AppleClamshellState", "-d", "4"]))

    thermal0 = 0

    def thermal_now() -> int:
        try:
            return _thermal_count()
        except (ThermalParseError, OSError, subprocess.SubprocessError):
            return thermal0

    def finish(code: int, reason: str, tok: Sequence[float], prod: str, end: Power) -> int:
        rec = build_record(
            ts=started.isoformat(timespec="seconds"),
            model=args.model,
            duration_s=int((datetime.now(JST) - started).total_seconds()),
            tok_s=tok,
            power=power0.raw,
            battery_start=power0.battery_pct,
            battery_end=end.battery_pct,
            thermal_events=max(0, thermal_now() - thermal0),
            production=prod,
            code=code,
            reason=reason,
        )
        append_record(out, rec)
        print(json.dumps(rec, ensure_ascii=False))
        return code

    if args.dry_run:
        busy = api_busy_jobs(list(http_json("GET", f"{args.api}/api/v1/jobs")["jobs"]))
        print(
            f"[verify] dry-run (生成なし・追記なし): on_ac={power0.on_ac}"
            f" battery={power0.battery_pct}"
            f" lid_closed={closed} busy={busy} deadline={deadline:%m-%d %H:%M} model={args.model}"
        )
        return EXIT_OK
    try:
        thermal0 = _thermal_count()
    except (ThermalParseError, OSError, subprocess.SubprocessError) as e:
        return finish(EXIT_ERROR, f"熱の判定を読めない: {e}"[:200], [], "not checked", power0)
    if not power0.on_ac or closed:
        code, why = decide(on_ac=power0.on_ac, lid_is_closed=closed, tok_s=[], battery_drop=0)
        return finish(code, why, [], "not checked", power0)
    try:
        if not _wait_not_busy(args.api, deadline):
            return finish(EXIT_BUSY, "busy が締め切りまでに解けない", [], "busy", power0)
        tok = [_generate(args.model, seed) for seed in range(1, RUNS + 1)]
    except Exception as e:  # noqa: BLE001
        return finish(EXIT_ERROR, f"実行エラー: {type(e).__name__}: {e}"[:200], [], "error", power0)
    end = parse_pmset(_sh(["pmset", "-g", "batt"]))
    drop = (
        0
        if battery_drop_is_noise(power0, end)
        else (power0.battery_pct or 0) - (end.battery_pct or 0)
    )
    code, why = decide(on_ac=end.on_ac, lid_is_closed=closed, tok_s=tok, battery_drop=drop)
    return finish(code, why, tok, "idle (no running jobs)", end)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--deadline", required=True, help="busy を待つ壁時計の締め切り (JST HH:MM)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--api", default=DEFAULT_API)
    ap.add_argument("--out", default=str(OUT))
    return run(ap.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
