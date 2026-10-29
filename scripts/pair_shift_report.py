#!/usr/bin/env python3
"""群化の対の判定 (event_pair_shadow) のずれの報告 (P0-08) — SELECT のみ。

3 期 (切替前 / 軸の割当を替えた期 / 対の判定を戻した期) x cos 帯で ``llm_same``・
``ml_joined``・``rule_joined`` の率を出す。数値特徴の KS 統計量は**診断の補助**で、
原因の結論には使わない (結論は pair_same_input_control.py の同じ入力の対照でだけ書く)。

期の境界は設定の版履歴 (model_tiers) の作成時刻から取る。版は引数で指定する。

使い方:
    uv run python scripts/pair_shift_report.py --days 60 --v-ci65 42 --v-restore 45
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PERIODS = ("before", "middle", "after")
#: cos 帯の下端 (最後の帯は 1.0 まで)
COS_EDGES = (0.0, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0001)
CONFIG_KEY = "model_tiers"
_VERSION_SQL = "SELECT created_at FROM app_config_versions WHERE config_key=? AND version=?"
_ROWS_SQL = (
    "SELECT observed_at, cos, llm_same, ml_joined, rule_joined, features_json"
    " FROM event_pair_shadow WHERE observed_at>=? ORDER BY observed_at"
)


@dataclass(frozen=True)
class PairRow:
    observed_at: str
    cos: float
    llm_same: int | None
    ml_joined: int | None
    rule_joined: int
    features: dict[str, float]


def assign_period(observed_at: str, boundaries: tuple[str, str]) -> str:
    """ISO 文字列 (UTC) の比較で期を決める。boundaries = (中間期の始め, 戻した期の始め)。"""
    if observed_at < boundaries[0]:
        return "before"
    if observed_at < boundaries[1]:
        return "middle"
    return "after"


def cos_band(cos: float) -> str:
    for lo, hi in zip(COS_EDGES, COS_EDGES[1:], strict=False):
        if lo <= cos < hi:
            return f"{lo:.1f}-{min(hi, 1.0):.1f}"
    return "out"


def _rate(values: Sequence[int | None]) -> tuple[int, float | None]:
    known = [v for v in values if v is not None]
    return len(known), (sum(known) / len(known) if known else None)


def aggregate(
    rows: Sequence[PairRow], boundaries: tuple[str, str]
) -> dict[tuple[str, str], dict[str, Any]]:
    """(期, cos 帯) ごとの件数と 3 つの率。率の母数は欠測を除いた件数。"""
    groups: dict[tuple[str, str], list[PairRow]] = {}
    for r in rows:
        groups.setdefault((assign_period(r.observed_at, boundaries), cos_band(r.cos)), []).append(r)
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for key, rs in sorted(groups.items()):
        n_l, llm = _rate([r.llm_same for r in rs])
        n_m, ml = _rate([r.ml_joined for r in rs])
        _, rule = _rate([r.rule_joined for r in rs])
        out[key] = {
            "n": len(rs),
            "llm_n": n_l,
            "llm_same": llm,
            "ml_n": n_m,
            "ml_joined": ml,
            "rule_joined": rule,
        }
    return out


def ks_statistic(a: Sequence[float], b: Sequence[float]) -> float | None:
    """2 標本 KS 統計量 (最大の累積分布の差)。どちらかが空なら None。診断の補助。"""
    if not a or not b:
        return None
    xs, ys = sorted(a), sorted(b)
    i = j = 0
    d = 0.0
    while i < len(xs) and j < len(ys):
        v = min(xs[i], ys[j])
        while i < len(xs) and xs[i] <= v:
            i += 1
        while j < len(ys) and ys[j] <= v:
            j += 1
        d = max(d, abs(i / len(xs) - j / len(ys)))
    return d


def feature_ks(
    rows: Sequence[PairRow], boundaries: tuple[str, str], left: str, right: str
) -> dict[str, float]:
    """2 つの期の数値特徴ごとの KS 統計量 (大きい順)。"""
    by_period: dict[str, list[PairRow]] = {p: [] for p in PERIODS}
    for r in rows:
        by_period[assign_period(r.observed_at, boundaries)].append(r)
    names = sorted({k for r in rows for k in r.features})
    result: dict[str, float] = {}
    for name in names:
        a = [r.features[name] for r in by_period[left] if name in r.features]
        b = [r.features[name] for r in by_period[right] if name in r.features]
        d = ks_statistic(a, b)
        if d is not None:
            result[name] = d
    return dict(sorted(result.items(), key=lambda kv: -kv[1]))


def parse_row(raw: Sequence[Any]) -> PairRow:
    try:
        feats_raw = json.loads(raw[5]) if raw[5] else {}
    except (TypeError, ValueError):
        feats_raw = {}
    feats = {k: float(v) for k, v in feats_raw.items() if isinstance(v, int | float)}
    return PairRow(
        observed_at=str(raw[0]),
        cos=float(raw[1]),
        llm_same=None if raw[2] is None else int(raw[2]),
        ml_joined=None if raw[3] is None else int(raw[3]),
        rule_joined=int(raw[4] or 0),
        features=feats,
    )


def _fmt(v: float | None) -> str:
    return "  -  " if v is None else f"{v:5.3f}"


def format_report(
    agg: dict[tuple[str, str], dict[str, Any]], ks: dict[str, float], boundaries: tuple[str, str]
) -> str:
    lines = [f"期の境界 (UTC): middle>={boundaries[0]} after>={boundaries[1]}"]
    lines.append("period  band      n   llm_same(n)   ml_joined(n)  rule_joined")
    for (period, band), v in agg.items():
        lines.append(
            f"{period:<7} {band:<8} {v['n']:>5}  {_fmt(v['llm_same'])}({v['llm_n']:>4})"
            f"  {_fmt(v['ml_joined'])}({v['ml_n']:>4})  {_fmt(v['rule_joined'])}"
        )
    lines.append("KS (診断の補助。結論には使わない) before vs middle の上位 8:")
    for name, d in list(ks.items())[:8]:
        lines.append(f"  {name:<28} {d:5.3f}")
    return "\n".join(lines)


def main() -> int:
    from src.storage.db_backend import connect
    from src.storage.run_history import DEFAULT_DB_PATH

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--v-ci65", type=int, required=True, help="中間期の始めの版")
    ap.add_argument("--v-restore", type=int, required=True, help="戻した期の始めの版")
    args = ap.parse_args()
    since = (datetime.now(UTC) - timedelta(days=args.days)).isoformat()
    conn = connect(DEFAULT_DB_PATH)
    try:
        starts: list[str] = []
        for version in (args.v_ci65, args.v_restore):
            row = conn.execute(_VERSION_SQL, (CONFIG_KEY, version)).fetchone()
            if row is None:
                print(f"版 {version} が見つかりません", file=sys.stderr)
                return 2
            starts.append(str(row[0]))
        rows = [parse_row(r) for r in conn.execute(_ROWS_SQL, (since,)).fetchall()]
    finally:
        conn.close()
    boundaries = (starts[0], starts[1])
    print(
        format_report(
            aggregate(rows, boundaries),
            feature_ks(rows, boundaries, "before", "middle"),
            boundaries,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
