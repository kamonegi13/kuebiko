"""週次の配信の実績の集計 (WEEKLY-ROUTING-TOOL)。読むだけ (SELECT のみ)。

直近 7 日にチャンネル別に実際に配信された件数を、10-28 の週 (articles.created_at が
[2026-10-22, 2026-10-29) JST) のベースラインと ±2√n の帯で比べ、帯の外のチャンネルを列挙する。

  (a) articles.posted_channel (status='posted'。空は '(empty)' として別に数える)
  (b) routing_v2_shadow.new_channel (article_id で articles に結合して Grok を除く)

- ベースラインの週は Grok の欠落 (10-24〜28) を含むので、両側から body_source='grok' の記事と
  GROK-RECOVER の取り直しの run_id (--recover-runs、既定 data/mlx/.grok_recover_1029.runs.tsv の
  2 列目) を除く。Grok は別の表 (件数のみ、帯の判定なし) で追う。
- 設定の版の変更 (app_config_versions) は補助の一覧。判定には使わない。
- ``--axes stored`` 固定。i58 は Ollama を呼ぶので拒否する (この道具は LLM を呼ばない)。
- DB は db_backend 経由で DATABASE_URL の PG を読む。未設定なら失敗する (SQLite には落とさない)。

ホストの ``uv run`` には DATABASE_URL が無いので、コンテナで実行する:

    docker compose run --rm --no-deps -T kuebiko python scripts/weekly_routing_check.py
    docker compose run --rm --no-deps -T kuebiko \
        python scripts/weekly_routing_check.py --as-of 2026-11-09
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

JST = timezone(timedelta(hours=9))
BASELINE_START = datetime(2026, 10, 22, tzinfo=JST)
BASELINE_END = datetime(2026, 10, 29, tzinfo=JST)
WINDOW_DAYS = 7
BAND_SIGMA = 2.0
EMPTY_CHANNEL = "(empty)"
DEFAULT_RECOVER_RUNS = Path("data/mlx/.grok_recover_1029.runs.tsv")
RECOVER_RUN_ID_COLUMN = 1  # 単位キー / run_id / status / fetched / 時刻


@dataclass(frozen=True)
class BandRow:
    channel: str
    baseline: int
    current: int
    low: float
    high: float
    is_outside: bool


def band_for(baseline_n: int) -> tuple[float, float]:
    """ベースライン件数 n に対する ±2√n の帯 (下限は 0 で切る)。"""
    half = BAND_SIGMA * math.sqrt(max(baseline_n, 0))
    return max(baseline_n - half, 0.0), baseline_n + half


def compare_counts(baseline: Mapping[str, int], current: Mapping[str, int]) -> list[BandRow]:
    """チャンネル別にベースラインの帯と今週の件数を比べる (和集合、チャンネル名順)。"""
    rows: list[BandRow] = []
    for channel in sorted(set(baseline) | set(current)):
        base_n = baseline.get(channel, 0)
        cur_n = current.get(channel, 0)
        low, high = band_for(base_n)
        rows.append(
            BandRow(channel, base_n, cur_n, low, high, is_outside=not (low <= cur_n <= high))
        )
    return rows


def outside_channels(rows: Sequence[BandRow]) -> list[BandRow]:
    return [r for r in rows if r.is_outside]


@dataclass(frozen=True)
class ShareRow:
    channel: str
    baseline_share: float
    current_share: float
    low: float
    high: float
    is_outside: bool


def _shares(counts: Mapping[str, int]) -> tuple[dict[str, float], int]:
    total = sum(counts.values())
    if total <= 0:
        return {}, 0
    return {k: v / total for k, v in counts.items()}, total


def compare_shares(baseline: Mapping[str, int], current: Mapping[str, int]) -> list[ShareRow]:
    """チャンネルの割合を比べる (判定に使う、2026-10-31 利用者承認)。

    帯 = ベースラインの割合 p0 ± 2√(p0(1−p0)/N1) (N1 = 今週の総数)。収集量が全体に
    増減しただけの週は割合が変わらないので帯の外に出ない
    (件数の帯は量の変化で全チャンネルが外に出る)。
    """
    base_share, _ = _shares(baseline)
    cur_share, cur_total = _shares(current)
    rows: list[ShareRow] = []
    for channel in sorted(set(baseline) | set(current)):
        p0 = base_share.get(channel, 0.0)
        p1 = cur_share.get(channel, 0.0)
        half = BAND_SIGMA * math.sqrt(p0 * (1 - p0) / cur_total) if cur_total else 0.0
        low, high = max(p0 - half, 0.0), min(p0 + half, 1.0)
        rows.append(ShareRow(channel, p0, p1, low, high, is_outside=not (low <= p1 <= high)))
    return rows


def outside_shares(rows: Sequence[ShareRow]) -> list[ShareRow]:
    return [r for r in rows if r.is_outside]


def format_share_table(title: str, rows: Sequence[ShareRow]) -> str:
    lines = [
        f"## {title} — 割合 (判定)",
        f"{'channel':<14}{'baseline':>9}{'current':>9}  {'band':<16}verdict",
    ]
    for r in rows:
        band = f"{r.low:.3f}-{r.high:.3f}"
        verdict = "外" if r.is_outside else "内"
        lines.append(
            f"{r.channel:<14}{r.baseline_share:>9.3f}{r.current_share:>9.3f}  {band:<16}{verdict}"
        )
    return "\n".join(lines)


def require_nonempty_baseline(counts: Mapping[str, int], label: str) -> None:
    """ベースラインの週が空なら失敗 (空の帯で全件が帯の外になるのを防ぐ)。"""
    if sum(counts.values()) == 0:
        raise SystemExit(f"error: ベースラインの週が空です ({label})。範囲・DB を確認してください")


def window_for(as_of: datetime, days: int = WINDOW_DAYS) -> tuple[datetime, datetime]:
    return as_of - timedelta(days=days), as_of


def parse_recover_run_ids(text: str) -> frozenset[int]:
    """GROK-RECOVER の runs.tsv (単位キー TAB run_id TAB ...) から run_id を読む。"""
    ids: set[int] = set()
    for line in text.splitlines():
        cols = line.split("\t")
        if len(cols) > RECOVER_RUN_ID_COLUMN and cols[RECOVER_RUN_ID_COLUMN].strip().isdigit():
            ids.add(int(cols[RECOVER_RUN_ID_COLUMN].strip()))
    return frozenset(ids)


def check_axes(axes: str) -> str:
    """週次の入口は stored 固定。i58 等 (Ollama を呼ぶ経路) は拒む。"""
    if axes != "stored":
        raise SystemExit(
            f"error: --axes {axes} は週次では使えません (Ollama を呼ぶ)。--axes stored 固定です"
        )
    return axes


def require_database_url(env: Mapping[str, str]) -> None:
    if not env.get("DATABASE_URL"):
        raise SystemExit(
            "error: DATABASE_URL が未設定です (SQLite には落としません)。"
            " docker compose run --rm --no-deps -T kuebiko python scripts/weekly_routing_check.py"
        )


# ===== DB (SELECT のみ) =====


def _in_clause(run_ids: frozenset[int]) -> tuple[str, list[int]]:
    marks = ",".join("?" for _ in run_ids)
    return marks, sorted(run_ids)


def _to_counts(rows: Sequence[Any]) -> dict[str, int]:
    return {str(r[0]): int(r[1]) for r in rows}


def fetch_posted(
    conn: Any, start: datetime, end: datetime, run_ids: frozenset[int]
) -> dict[str, int]:
    run_sql, run_params = "", []
    if run_ids:
        marks, run_params = _in_clause(run_ids)
        run_sql = f" AND a.run_id NOT IN ({marks})"
    sql = (
        f"SELECT COALESCE(NULLIF(a.posted_channel, ''), '{EMPTY_CHANNEL}') AS ch, COUNT(*) AS n"
        " FROM articles a WHERE a.status = 'posted'"
        " AND a.created_at >= CAST(? AS timestamptz) AND a.created_at < CAST(? AS timestamptz)"
        f" AND a.body_source IS DISTINCT FROM 'grok'{run_sql} GROUP BY ch"
    )
    cur = conn.execute(sql, [start.isoformat(), end.isoformat(), *run_params])
    return _to_counts(cur.fetchall())


def fetch_shadow(
    conn: Any, start: datetime, end: datetime, run_ids: frozenset[int]
) -> dict[str, int]:
    run_sql, run_params = "", []
    if run_ids:
        marks, run_params = _in_clause(run_ids)
        run_sql = f" OR a.run_id IN ({marks})"
    sql = (
        "SELECT s.new_channel AS ch, COUNT(*) AS n FROM routing_v2_shadow s"
        " WHERE CAST(s.created_at AS timestamptz) >= CAST(? AS timestamptz)"
        " AND CAST(s.created_at AS timestamptz) < CAST(? AS timestamptz)"
        " AND NOT EXISTS (SELECT 1 FROM articles a WHERE a.article_id = s.article_id"
        f" AND (a.body_source = 'grok'{run_sql}))"
        " GROUP BY s.new_channel"
    )
    cur = conn.execute(sql, [start.isoformat(), end.isoformat(), *run_params])
    return _to_counts(cur.fetchall())


def fetch_grok(conn: Any, start: datetime, end: datetime) -> dict[str, int]:
    """Grok は別の表 (status='posted' の posted_channel 別。帯の判定はしない)。"""
    sql = (
        f"SELECT COALESCE(NULLIF(a.posted_channel, ''), '{EMPTY_CHANNEL}') AS ch, COUNT(*) AS n"
        " FROM articles a WHERE a.status = 'posted' AND a.body_source = 'grok'"
        " AND a.created_at >= CAST(? AS timestamptz) AND a.created_at < CAST(? AS timestamptz)"
        " GROUP BY ch"
    )
    cur = conn.execute(sql, [start.isoformat(), end.isoformat()])
    return _to_counts(cur.fetchall())


def fetch_config_changes(conn: Any, start: datetime, end: datetime) -> list[tuple[str, int, int]]:
    """窓内に版が進んだ設定キー: (key, 最小版, 最大版)。補助の一覧。"""
    sql = (
        "SELECT config_key, MIN(version) AS v0, MAX(version) AS v1 FROM app_config_versions"
        " WHERE CAST(created_at AS timestamptz) >= CAST(? AS timestamptz)"
        " AND CAST(created_at AS timestamptz) < CAST(? AS timestamptz)"
        " GROUP BY config_key ORDER BY config_key"
    )
    cur = conn.execute(sql, [start.isoformat(), end.isoformat()])
    return [(str(r[0]), int(r[1]), int(r[2])) for r in cur.fetchall()]


# ===== 表示 =====


def format_table(title: str, rows: Sequence[BandRow]) -> str:
    lines = [f"## {title}", f"{'channel':<14}{'baseline':>9}{'current':>9}  {'band':<16}verdict"]
    for r in rows:
        band = f"{r.low:.1f}-{r.high:.1f}"
        verdict = "OUTSIDE" if r.is_outside else "ok"
        lines.append(f"{r.channel:<14}{r.baseline:>9}{r.current:>9}  {band:<16}{verdict}")
    names = ", ".join(r.channel for r in outside_channels(rows)) or "なし"
    lines.append(f"帯の外: {names}")
    return "\n".join(lines)


def format_grok(baseline: Mapping[str, int], current: Mapping[str, int]) -> str:
    lines = ["## Grok (別の表。帯の判定なし)", f"{'channel':<14}{'baseline':>9}{'current':>9}"]
    for ch in sorted(set(baseline) | set(current)):
        lines.append(f"{ch:<14}{baseline.get(ch, 0):>9}{current.get(ch, 0):>9}")
    return "\n".join(lines)


def format_config(changes: Sequence[tuple[str, int, int]]) -> str:
    lines = ["## 設定の版の変更 (補助。判定には使わない)"]
    lines += [f"{k}: v{v0}..v{v1}" for k, v0, v1 in changes] or ["変更なし"]
    return "\n".join(lines)


def _parse_as_of(text: str | None) -> datetime:
    if text is None:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=JST)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--axes", default="stored", help="stored 固定 (i58 等は拒否)")
    p.add_argument("--as-of", default=None, help="窓の終端 (ISO、tz 無しは JST)。既定は現在")
    p.add_argument("--recover-runs", type=Path, default=DEFAULT_RECOVER_RUNS)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    check_axes(args.axes)
    require_database_url(os.environ)
    run_ids = (
        parse_recover_run_ids(args.recover_runs.read_text())
        if args.recover_runs.exists()
        else frozenset()
    )
    cur_start, cur_end = window_for(_parse_as_of(args.as_of))

    from src.storage import db_backend  # noqa: PLC0415 (DATABASE_URL 検査の後で import)

    conn = db_backend.connect()
    try:
        base_posted = fetch_posted(conn, BASELINE_START, BASELINE_END, run_ids)
        require_nonempty_baseline(base_posted, "articles.posted_channel")
        base_shadow = fetch_shadow(conn, BASELINE_START, BASELINE_END, run_ids)
        require_nonempty_baseline(base_shadow, "routing_v2_shadow.new_channel")
        cur_posted = fetch_posted(conn, cur_start, cur_end, run_ids)
        cur_shadow = fetch_shadow(conn, cur_start, cur_end, run_ids)
        posted = compare_counts(base_posted, cur_posted)
        shadow = compare_counts(base_shadow, cur_shadow)
        posted_share = compare_shares(base_posted, cur_posted)
        shadow_share = compare_shares(base_shadow, cur_shadow)
        grok_base = fetch_grok(conn, BASELINE_START, BASELINE_END)
        grok_cur = fetch_grok(conn, cur_start, cur_end)
        config = fetch_config_changes(conn, cur_start, cur_end)
    finally:
        conn.close()

    fmt = "%Y-%m-%d %H:%M"
    print(f"今週: {cur_start.astimezone(JST):{fmt}} - {cur_end.astimezone(JST):{fmt}} JST")
    print(
        f"ベースライン: {BASELINE_START:%Y-%m-%d} - {BASELINE_END:%Y-%m-%d} JST"
        f" (Grok と取り直し {len(run_ids)} run を除外)"
    )
    print(format_share_table("articles.posted_channel (status=posted)", posted_share))
    print(format_share_table("routing_v2_shadow.new_channel", shadow_share))
    print("(以下の件数の ±2√n の帯は参考。収集量の増減でも外に出るので判定に使わない)")
    print(format_table("articles.posted_channel (status=posted)", posted))
    print(format_table("routing_v2_shadow.new_channel", shadow))
    print(format_grok(grok_base, grok_cur))
    print(format_config(config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
