#!/usr/bin/env python3
"""生成物の長さの週次レポート (P0-09、規律 R1 の「欄ごとの p90」の基線) — SELECT のみ。

欄ごとの文字数の p50 / p90 / 最大を出す。対象は Spotlight (見出し・見通し)・状況総括の節・
週次 recap・SIR 要点 (daily_briefs.payload)・事象ニュースの見出し・要点・detect の claim。
``llm_usage.hit_token_cap`` の列がある場合 (P0-16 以後) は上限到達の率も出す。

使い方:
    uv run python scripts/generation_length_report.py --days 7 [--json]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

JST = ZoneInfo("Asia/Tokyo")

#: 欄名 -> (SQL, 窓の始端を 1 つ取る)。値が文字列の列は文字数、JSON の列は extractor で取り出す
_TEXT_FIELDS: dict[str, str] = {
    "spotlight_headline": "SELECT headline FROM pir_spotlight WHERE generated_at>=?",
    "spotlight_outlook": "SELECT outlook FROM pir_spotlight WHERE generated_at>=?",
    "synthesis_headline": "SELECT headline FROM status_synthesis WHERE generated_at>=?",
    "synthesis_weight": "SELECT weight_section FROM status_synthesis WHERE generated_at>=?",
    "synthesis_chain": "SELECT chain_section FROM status_synthesis WHERE generated_at>=?",
    "synthesis_cog": "SELECT cog_section FROM status_synthesis WHERE generated_at>=?",
    "synthesis_spillover": "SELECT spillover_section FROM status_synthesis WHERE generated_at>=?",
    "synthesis_pir": "SELECT pir_section FROM status_synthesis WHERE generated_at>=?",
    "weekly_recap": "SELECT recap_text FROM weekly_recaps WHERE generated_at>=?",
    "eventnews_headline": "SELECT headline FROM event_item_versions WHERE generated_at>=?",
    "detect_claim": "SELECT claim FROM detect_bundles WHERE run_at>=? AND claim<>''",
}
_BRIEF_SQL = "SELECT payload FROM daily_briefs WHERE generated_at>=?"
_EVENT_BODY_SQL = "SELECT body_json FROM event_item_versions WHERE generated_at>=?"


def percentile(sorted_values: Sequence[int], p: float) -> int:
    """最近順位法 (空は 0)。"""
    if not sorted_values:
        return 0
    rank = max(1, math.ceil(p * len(sorted_values)))
    return sorted_values[rank - 1]


def stats(values: Sequence[int]) -> dict[str, int]:
    s = sorted(values)
    return {
        "n": len(s),
        "p50": percentile(s, 0.5),
        "p90": percentile(s, 0.9),
        "max": s[-1] if s else 0,
    }


def _loads(raw: object) -> Any:
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError):
        return None


def sir_summary_lengths(payloads: Sequence[object]) -> list[int]:
    """daily_briefs.payload の ``pir[].summary`` (SIR 別の要点) の文字数。"""
    out: list[int] = []
    for raw in payloads:
        data = _loads(raw)
        items = data.get("pir") if isinstance(data, dict) else None
        if not isinstance(items, list):
            continue
        out.extend(
            len(i["summary"])
            for i in items
            if isinstance(i, dict) and isinstance(i.get("summary"), str)
        )
    return out


def event_body_lengths(bodies: Sequence[object]) -> tuple[list[int], list[int]]:
    """事象ニュースの body_json から (bluf の文字数, key_points 1 件ごとの文字数)。"""
    blufs: list[int] = []
    points: list[int] = []
    for raw in bodies:
        data = _loads(raw)
        if not isinstance(data, dict):
            continue
        if isinstance(data.get("bluf"), str):
            blufs.append(len(data["bluf"]))
        kp = data.get("key_points")
        if isinstance(kp, list):
            points.extend(len(p) for p in kp if isinstance(p, str))
    return blufs, points


def collect(conn: Any, since: str) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for name, sql in _TEXT_FIELDS.items():
        out[name] = [len(str(r[0] or "")) for r in conn.execute(sql, (since,)).fetchall()]
    out["sir_focus_summary"] = sir_summary_lengths(
        [r[0] for r in conn.execute(_BRIEF_SQL, (since,)).fetchall()]
    )
    blufs, points = event_body_lengths(
        [r[0] for r in conn.execute(_EVENT_BODY_SQL, (since,)).fetchall()]
    )
    out["eventnews_bluf"] = blufs
    out["eventnews_key_point"] = points
    return out


def token_cap_rate(conn: Any, since: str) -> dict[str, Any]:
    """``llm_usage.hit_token_cap`` の率。列が無い (P0-16 前) ときは未実装として返す。"""
    try:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(hit_token_cap), 0) FROM llm_usage WHERE ts>=?",
            (since,),
        ).fetchone()
    except Exception as e:  # noqa: BLE001 — 列が無いのは想定内。握りつぶさず理由を返す
        return {"available": False, "reason": type(e).__name__}
    total, hit = int(row[0] or 0), int(row[1] or 0)
    return {"available": True, "calls": total, "hit": hit, "rate": hit / total if total else None}


def format_report(
    report: dict[str, dict[str, int]], cap: dict[str, Any], days: int, now: datetime
) -> str:
    lines = [
        f"生成物の長さ (文字数) 直近 {days} 日 / 作成 {now.astimezone(JST):%Y-%m-%d %H:%M} JST"
    ]
    lines.append(f"{'欄':<24}{'n':>6}{'p50':>7}{'p90':>7}{'max':>7}")
    for name, s in report.items():
        lines.append(f"{name:<24}{s['n']:>6}{s['p50']:>7}{s['p90']:>7}{s['max']:>7}")
    if cap.get("available"):
        rate = cap["rate"]
        lines.append(
            f"hit_token_cap: {cap['hit']}/{cap['calls']} ({'-' if rate is None else f'{rate:.1%}'})"
        )
    else:
        lines.append(f"hit_token_cap: 未実装 (P0-16 の後に出る) [{cap.get('reason')}]")
    return "\n".join(lines)


def build_report(conn: Any, since: str) -> tuple[dict[str, dict[str, int]], dict[str, Any]]:
    return {k: stats(v) for k, v in collect(conn, since).items()}, token_cap_rate(conn, since)


def main() -> int:
    from src.storage.db_backend import connect
    from src.storage.run_history import DEFAULT_DB_PATH

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    now = datetime.now(UTC)
    since = (now - timedelta(days=args.days)).isoformat()
    conn = connect(DEFAULT_DB_PATH)
    try:
        report, cap = build_report(conn, since)
    finally:
        conn.close()
    if args.json:
        print(json.dumps({"fields": report, "hit_token_cap": cap}, ensure_ascii=False, indent=2))
    else:
        print(format_report(report, cap, args.days, now))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
