#!/usr/bin/env python3
"""detect 凍結審判 (build_detect_goldset.py) の結果を **事象単位** に畳んで集計する。

記事単位の数字を成績として語らない (2026-08-31「ペア単位 88% を製品の成績として語らない」
と同型)。同一事象の記事が 3 本あれば 1 本開けば正解。事象の単位は ``event_items``
(``merged_into`` を辿った正規 id)、紐づかない記事は自身を 1 事象と数える。

比較する 3 者:
- 現行 detect: 同一事象のどれかの記事が ``decision='opened'`` (detect_labels.json、全記事)
- 独立採点 (open_score 上位): 層 scoring / both
- 審判: gold_open (開設) / watch (見張り) / それ以外 (見送り)

使い方 (DB を読むので **コンテナ内で**。ホストは空 SQLite へ落ちて 0 件になる):
    docker exec kuebiko python scripts/analyze_detect_goldset.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.storage.run_history import RunHistoryRepository  # noqa: E402

GOLD = Path("data/mlx/detect_goldset.jsonl")
LABELS = Path("data/mlx/detect_labels.json")
_MERGE_DEPTH_MAX = 5
_CHUNK = 200


@dataclass(frozen=True)
class EventRow:
    """事象 1 件の畳み込み結果。"""

    event_id: str
    n_gold_articles: int
    cur_open: bool
    scoring_top: bool
    in_incumbent: bool
    random_only: bool
    gold_open: bool
    watch: bool
    importance: int
    trackable: int

    @property
    def verdict(self) -> str:
        if self.gold_open:
            return "open"
        return "watch" if self.watch else "drop"


def fold_events(
    gold: dict[str, dict[str, Any]],
    event_of: dict[str, str],
    members: dict[str, list[str]],
    opened_articles: set[str],
) -> list[EventRow]:
    """記事単位の審判を事象単位へ畳む (純粋関数、テスト対象)。

    - 審判の open / watch は事象内の記事の OR、importance / trackable は max
    - 現行の開設は **事象の全メンバー記事** (審判に出していない記事も含む) の OR
    """
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for article_id, row in gold.items():
        grouped[event_of.get(article_id, article_id)].append(row)
    out: list[EventRow] = []
    for event_id, rows in grouped.items():
        strata = {str(r["stratum"]) for r in rows}
        member_ids = members.get(event_id, [event_id])
        out.append(
            EventRow(
                event_id=event_id,
                n_gold_articles=len(rows),
                cur_open=any(m in opened_articles for m in member_ids),
                scoring_top=bool(strata & {"scoring", "both"}),
                in_incumbent=bool(strata & {"incumbent", "both"}),
                random_only=strata == {"random"},
                gold_open=any(bool(r["gold_open"]) for r in rows),
                watch=any(bool(r["watch"]) for r in rows),
                importance=max(int(r["importance"]) for r in rows),
                trackable=max(int(r["trackable"]) for r in rows),
            )
        )
    return out


def _chunked_in(conn: Any, sql_template: str, ids: list[str]) -> list[Any]:
    rows: list[Any] = []
    for i in range(0, len(ids), _CHUNK):
        chunk = ids[i : i + _CHUNK]
        placeholders = ",".join("?" * len(chunk))
        rows.extend(conn.execute(sql_template.format(ph=placeholders), tuple(chunk)).fetchall())
    return rows


def _canonical(item_id: str, merged_into: dict[str, str | None]) -> str:
    cur = item_id
    for _ in range(_MERGE_DEPTH_MAX):
        nxt = merged_into.get(cur)
        if not nxt:
            break
        cur = nxt
    return cur


def load_event_mapping(
    repo: RunHistoryRepository, article_ids: list[str]
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """記事 → 正規事象 id、正規事象 id → 全メンバー記事 を DB から引く。"""
    with repo._connect() as conn:  # noqa: SLF001 — 評価スクリプトの接続 seam 共有
        first = _chunked_in(
            conn,
            "SELECT article_id AS a, item_id AS i FROM event_item_members "
            "WHERE article_id IN ({ph})",  # noqa: S608
            article_ids,
        )
        raw_item_of = {str(r["a"]): str(r["i"]) for r in first}
        merged: dict[str, str | None] = {}
        frontier = sorted(set(raw_item_of.values()))
        for _ in range(_MERGE_DEPTH_MAX):
            if not frontier:
                break
            items = _chunked_in(
                conn,
                "SELECT id, merged_into FROM event_items WHERE id IN ({ph})",  # noqa: S608
                frontier,
            )
            frontier = []
            for r in items:
                merged[str(r["id"])] = str(r["merged_into"]) if r["merged_into"] else None
                if r["merged_into"] and str(r["merged_into"]) not in merged:
                    frontier.append(str(r["merged_into"]))
        event_of = {a: _canonical(i, merged) for a, i in raw_item_of.items()}
        member_rows = _chunked_in(
            conn,
            "SELECT item_id AS i, article_id AS a FROM event_item_members WHERE item_id IN ({ph})",  # noqa: S608
            sorted(set(event_of.values())),
        )
    members: dict[str, list[str]] = defaultdict(list)
    for r in member_rows:
        members[str(r["i"])].append(str(r["a"]))
    return event_of, dict(members)


def _line(name: str, rows: list[EventRow]) -> str:
    c = Counter(r.verdict for r in rows)
    n = len(rows)
    pct = f"{c['open'] / n * 100:3.0f}%" if n else "  -"
    return (
        f"{name:<26s} 事象 {n:3d} | 審判=開設 {c['open']:3d} ({pct}) "
        f"見張り {c['watch']:3d} 見送り {c['drop']:3d}"
    )


def report(rows: list[EventRow], held_out_days: int, held_out_articles: int) -> str:
    groups = [
        ("現行が開設", [r for r in rows if r.cur_open]),
        ("独立採点 上位", [r for r in rows if r.scoring_top]),
        ("無作為 (両方非該当)", [r for r in rows if r.random_only and not r.cur_open]),
        ("全体", rows),
    ]
    lines = [f"事象 {len(rows)} 件 (複数記事の事象 {sum(r.n_gold_articles > 1 for r in rows)})"]
    lines += [_line(n, g) for n, g in groups]
    opened = [r for r in rows if r.gold_open]
    lines.append(
        f"審判=開設 {len(opened)} 事象: 現行が開設 {sum(r.cur_open for r in opened)} / "
        f"独立採点上位 {sum(r.scoring_top for r in opened)} / "
        f"どちらも非該当 {sum(not r.cur_open and not r.scoring_top for r in opened)}"
    )
    selectors: list[tuple[str, Callable[[EventRow], bool]]] = [
        ("現行開設", lambda r: r.cur_open),
        ("審判開設", lambda r: r.gold_open),
    ]
    for axis in ("trackable", "importance"):
        for name, sel in selectors:
            c = Counter(getattr(r, axis) for r in rows if sel(r))
            lines.append(f"  {axis} {name}: {dict(sorted(c.items()))}")
    rand = [r for r in rows if r.random_only and not r.cur_open]
    if rand and held_out_days:
        rate = sum(r.gold_open for r in rand) / len(rand)
        missed = rate * (held_out_articles - sum(r.cur_open for r in rows))
        lines.append(
            f"外挿: 無作為層の開設率 {rate * 100:.1f}% × 現行非開設 → "
            f"取りこぼし ≈{missed:.0f} 事象 / "
            f"{held_out_days} 日 ≈ {missed / held_out_days:.1f}/日 (現行の開設 "
            f"{sum(r.cur_open for r in rows) / held_out_days:.1f}/日)"
        )
    return "\n".join(lines)


def held_out_days(held_out: list[dict[str, Any]]) -> int:
    """held-out の暦日数 (両端含む)。"""
    if not held_out:
        return 0
    first = date.fromisoformat(str(held_out[0]["run_at"])[:10])
    last = date.fromisoformat(str(held_out[-1]["run_at"])[:10])
    return (last - first).days + 1


def main() -> int:
    gold = {
        str(json.loads(line)["article_id"]): json.loads(line)
        for line in GOLD.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    labels: list[dict[str, Any]] = json.loads(LABELS.read_text(encoding="utf-8"))
    labels.sort(key=lambda r: str(r["run_at"]))
    held_out = labels[int(len(labels) * 0.7) :]
    opened = {str(r["article_id"]) for r in labels if r["decision"] == "opened"}
    days = (
        (int(str(held_out[-1]["run_at"])[8:10]) - int(str(held_out[0]["run_at"])[8:10])) % 31 + 1
        if held_out
        else 0
    )
    event_of, members = load_event_mapping(RunHistoryRepository(), sorted(gold))
    if not event_of:
        print("⚠ 事象への紐づけが 0 件 — ホスト実行 (空 SQLite) を疑う", file=sys.stderr)
        return 1
    rows = fold_events(gold, event_of, members, opened)
    print(report(rows, days, len(held_out)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
