#!/usr/bin/env python3
"""triage importance の較正監査 (P4, 2026-07-11 品質考察の常設化)。

総量レベルの importance 分布は**構成変化** (geopolitical/research の量変動、収集修復による
カテゴリ混合の変化) に汚染され、較正ドリフトの指標にならない (2026-07-11 実測: 週次 medium
36→59% の振れは構成が主因で、カテゴリ内の較正は安定だった)。本スクリプトは較正を
**カテゴリ内**で測り、構成 (mix) と分離して表示する:

  1. カテゴリ内較正: cyber 脅威カテゴリ別の週次 high/medium 率 (これが較正の本体)
  2. 構成: カテゴリ混合の週次シェア (総量分布の振れの説明側)
  3. 悪用シグナル検査: 悪用語を含む vulnerability/advisory が high になっているか
     (B-cal2 の「high=実悪用/KEV/0day のみ」ガードの過剰抑制/過剰昇格の検知)
  4. 判定: 隣接週でカテゴリ内 high 率が閾値超で振れたら WARN (n が小さい週は除外)

Usage (production PG、コンテナ内):
    docker exec kuebiko python /app/scripts/audit_triage_calibration.py
    docker exec kuebiko python /app/scripts/audit_triage_calibration.py --weeks 12

read-only。週次集計は PG の date_trunc/to_char を使う (production 用。dev SQLite は対象外)。
基準の系譜: [[phase_b_calibration]] (B-cal/B-cal2) → [[synthesis_quality_audit_2026_07_11]] P4。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.storage.db_backend import connect  # noqa: E402

# 較正を測る対象 = cyber 脅威カテゴリ (importance が actionability 軸のもの)。
# geopolitical/research/policy は importance の軸が違う (戦略的重要度/原則low) ため分離。
THREAT_CATEGORIES: tuple[str, ...] = (
    "vulnerability",
    "malware",
    "apt",
    "breach",
    "incident",
    "advisory",
)
# 「悪用の実在」シグナル (title)。B-cal2 の high 基準 = 実悪用/KEV/0day。
_EXPLOIT_SIGNAL_RE = r"actively exploited|in the wild|zero-?day|悪用|KEV"

# 判定閾値の既定値 (CLI で上書き可)
_DEFAULT_WEEKS = 8
_DEFAULT_MIN_N = 30  # 週次 n がこれ未満のセルは判定から除外 (少数標本の見かけ振れ)
_DEFAULT_SWING_PP = 25  # 隣接週の high 率変化 (pp) がこれ超で WARN
_EXPLOIT_HIGH_FLOOR_PCT = 50  # 悪用シグナル付きの high 率がこれ未満なら過剰抑制 WARN


@dataclass(frozen=True)
class WeekCell:
    """カテゴリ×週の較正セル。"""

    week: str
    category: str
    n: int
    high_pct: float


def detect_calibration_swings(cells: list[WeekCell], *, min_n: int, swing_pp: float) -> list[str]:
    """隣接週でカテゴリ内 high 率が swing_pp を超えて動いたセルを WARN 文字列で返す。

    n < min_n の週は判定から除外 (少数標本の見かけ振れを較正ドリフトと誤認しない)。
    """
    warns: list[str] = []
    by_cat: dict[str, list[WeekCell]] = {}
    for c in sorted(cells, key=lambda c: (c.category, c.week)):
        if c.n >= min_n:
            by_cat.setdefault(c.category, []).append(c)
    for cat, seq in sorted(by_cat.items()):
        for prev, cur in zip(seq, seq[1:], strict=False):
            delta = cur.high_pct - prev.high_pct
            if abs(delta) > swing_pp:
                warns.append(
                    f"WARN {cat}: high率 {prev.week} {prev.high_pct:.0f}% → "
                    f"{cur.week} {cur.high_pct:.0f}% ({delta:+.0f}pp, n={prev.n}→{cur.n})"
                )
    return warns


def _fetch_cells(conn: object, weeks: int) -> list[WeekCell]:
    ph = ",".join("?" for _ in THREAT_CATEGORIES)
    rows = conn.execute(  # type: ignore[attr-defined]
        "SELECT to_char(date_trunc('week', created_at AT TIME ZONE 'Asia/Tokyo'),'MM-DD') wk,"
        " category, count(*) n,"
        " round(100.0*count(*) FILTER (WHERE importance='high')/count(*),1) hi"
        " FROM articles"
        " WHERE created_at > now() - (? || ' days')::interval AND status='posted'"
        f" AND category IN ({ph})"  # noqa: S608 — ph は ? 固定
        " GROUP BY 1,2 ORDER BY 2,1",
        [str(weeks * 7), *THREAT_CATEGORIES],
    ).fetchall()
    return [
        WeekCell(week=str(r[0]), category=str(r[1]), n=int(r[2]), high_pct=float(r[3]))
        for r in rows
    ]


def _print_composition(conn: object, weeks: int) -> None:
    rows = conn.execute(  # type: ignore[attr-defined]
        "SELECT to_char(date_trunc('week', created_at AT TIME ZONE 'Asia/Tokyo'),'MM-DD') wk,"
        " count(*) n,"
        " round(100.0*count(*) FILTER (WHERE category IN"
        " ('vulnerability','malware','apt','breach','incident','advisory'))/count(*),0) threat,"
        " round(100.0*count(*) FILTER (WHERE category='geopolitical')/count(*),0) geo,"
        " round(100.0*count(*) FILTER (WHERE category='research')/count(*),0) research"
        " FROM articles"
        " WHERE created_at > now() - (? || ' days')::interval AND status='posted'"
        " GROUP BY 1 ORDER BY 1",
        [str(weeks * 7)],
    ).fetchall()
    print("\n== 構成 (総量分布の振れはここで説明されるべき) ==")
    print("week  | n     | threat% | geo% | research%")
    for r in rows:
        print(f"{r[0]} | {r[1]:>5} | {r[2]:>6} | {r[3]:>4} | {r[4]:>4}")


def _print_exploit_check(conn: object) -> list[str]:
    rows = conn.execute(  # type: ignore[attr-defined]
        "SELECT importance, count(*) FROM articles"
        " WHERE created_at > now() - interval '14 days' AND status='posted'"
        " AND category IN ('vulnerability','advisory')"
        " AND title ~* ? GROUP BY 1",
        [_EXPLOIT_SIGNAL_RE],
    ).fetchall()
    counts = {str(r[0]): int(r[1]) for r in rows}
    total = sum(counts.values())
    high_pct = 100.0 * counts.get("high", 0) / total if total else 0.0
    print("\n== 悪用シグナル検査 (直近14日, vulnerability/advisory × 悪用語 in title) ==")
    print(f"  {counts} → high率 {high_pct:.0f}%")
    warns: list[str] = []
    if total >= 10 and high_pct < _EXPLOIT_HIGH_FLOOR_PCT:
        warns.append(
            f"WARN 悪用シグナル付きの high 率 {high_pct:.0f}% < {_EXPLOIT_HIGH_FLOOR_PCT}%"
            " — _cap_vuln_importance の過剰抑制を疑う (非highの実タイトルを目視すること)"
        )
        sample = conn.execute(  # type: ignore[attr-defined]
            "SELECT importance, left(title,90) FROM articles"
            " WHERE created_at > now() - interval '14 days' AND status='posted'"
            " AND category IN ('vulnerability','advisory') AND title ~* ?"
            " AND importance <> 'high' ORDER BY created_at DESC LIMIT 10",
            [_EXPLOIT_SIGNAL_RE],
        ).fetchall()
        for r in sample:
            print(f"    [{r[0]}] {r[1]}")
    return warns


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--weeks", type=int, default=_DEFAULT_WEEKS)
    ap.add_argument("--min-n", type=int, default=_DEFAULT_MIN_N)
    ap.add_argument("--swing-pp", type=float, default=_DEFAULT_SWING_PP)
    args = ap.parse_args()

    conn = connect()
    try:
        cells = _fetch_cells(conn, args.weeks)
        print("== カテゴリ内較正 (cyber 脅威カテゴリ, 週次 high率) ==")
        print("week  | category      | n    | high%")
        for c in sorted(cells, key=lambda c: (c.category, c.week)):
            print(f"{c.week} | {c.category:<13} | {c.n:>4} | {c.high_pct:>5.1f}")

        _print_composition(conn, args.weeks)

        warns = detect_calibration_swings(cells, min_n=args.min_n, swing_pp=args.swing_pp)
        warns += _print_exploit_check(conn)

        print("\n== 判定 ==")
        if warns:
            for w in warns:
                print(" ", w)
            return 1
        print("  OK: カテゴリ内較正に閾値超の振れなし・悪用シグナルの high 率健全")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
