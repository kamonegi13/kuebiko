"""統合の段の入力を学習時の形に戻す変更 (EVENTNEWS_MERGE_REAL_KIND) の前後比較 (読み取り専用)。

正解つきの過去の統合 (``--labels``: absorbed / target / label=same|different|boundary を持つ
jsonl。data/eval/c4_labels.jsonl など) を、本番の関数 (``approve_pairs`` と ``plan_merges``) で
再現し、変更前後で **統合が成立するか** を比べる。

変えるのは 2 つだけ (他は本番と同じ):
  kind   : 種別を全件 "other" (現行) / DB の実値 (REAL_KIND)
  hub    : ハブ関門 (EVENTNEWS_MERGE_HUB_GATE) の on/off — REAL_KIND の効果と**分けて**読むため
           4 通り (kind × hub) を出す。

⚠ 近似: 頻出の名前の共有の辺は見ない (4 通りで共通)・頻出ガードの分母は現在窓・統合先は
「被吸収の時点までに入っていた記事」。要約埋込が無い記事は無いまま (Ollama を呼ばない)。
したがって「欠けた要約埋込を埋める」効果は含まない — 被覆だけを報告する。

⚠ 本番 DB は SELECT のみ。接続は PGOPTIONS で read-only にし、スキーマ作成 (DDL) も止める。

使い方:
  set -a; . ./.env; set +a
  DATABASE_URL="postgresql://kuebiko:${POSTGRES_PASSWORD}@127.0.0.1:5433/kuebiko" \\
  uv run python scripts/merge_input_replay.py --labels <c4_labels.jsonl> --out <result.jsonl>
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# 本番の既定 (compose)。import 前に置く — 一部は import 時に読まれる
os.environ.setdefault("EVENTNEWS_ORG_NORM", "1")
os.environ.setdefault("EVENTNEWS_MASKED_VICTIM_GATE", "1")
os.environ.setdefault("EVENTNEWS_MERGE_REPORT_PERIOD_GATE", "1")
os.environ.setdefault("PGOPTIONS", "-c default_transaction_read_only=on")

import numpy as np  # noqa: E402

import src.storage.db_backend as _db  # noqa: E402

_db.ensure_pg_schema = lambda *a, **k: None  # DDL を流さない

from src.eventnews import pair_model  # noqa: E402
from src.eventnews.grouping import build_join_entities  # noqa: E402
from src.eventnews.merge import edge_inputs, plan_merges  # noqa: E402
from src.eventnews.models import ENTITY_FREQ_WINDOW_HOURS  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.ui.services import eventnews_merge_job as job  # noqa: E402
from src.ui.services.eventnews_hourly_job import (  # noqa: E402
    _entity_counts,
    _load_members,
    _load_vectors,
)

RUN_WINDOW_MIN = 10
VARIANTS = list(itertools.product((False, True), (False, True)))  # (real_kind, hub_gate)


def _q(repo: RunHistoryRepository, sql: str, params: list[Any] | None = None) -> list[Any]:
    with repo._connect() as conn:  # noqa: SLF001 — 読み取りのみ
        return list(conn.execute(sql, params or []).fetchall())


def _side_members(repo: RunHistoryRepository, item: str) -> list[str]:
    rows = _q(repo, "SELECT article_id FROM event_item_members WHERE item_id=?", [item])
    return [str(r[0]) for r in rows]


def run_group(
    repo: RunHistoryRepository, row: dict[str, Any]
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """同じ実行で統合先へ吸収された事象 + 統合先 (それ以前の吸収分は統合先に含める)。"""
    t0 = datetime.fromisoformat(row["merged_at"])
    lo = (t0 - timedelta(minutes=RUN_WINDOW_MIN)).isoformat()
    hi = (t0 + timedelta(minutes=RUN_WINDOW_MIN)).isoformat()
    target = row["target"]
    same_run = _q(
        repo,
        "SELECT id FROM event_items WHERE merged_into=? AND updated_at>=? AND updated_at<=?",
        [target, lo, hi],
    )
    earlier = _q(
        repo,
        "SELECT m.article_id FROM event_item_members m JOIN event_items i ON i.id=m.item_id"
        " WHERE i.merged_into=? AND i.updated_at<?",
        [target, lo],
    )
    own = _q(
        repo,
        "SELECT article_id FROM event_item_members WHERE item_id=? AND join_signal<>'retro_merge'",
        [target],
    )
    items = {str(r[0]): _side_members(repo, str(r[0])) for r in same_run}
    items[target] = sorted({str(r[0]) for r in own} | {str(r[0]) for r in earlier})
    marks = ",".join("?" for _ in items)
    first = {
        str(r[0]): str(r[1])
        for r in _q(
            repo,
            f"SELECT id, first_reported_at FROM event_items WHERE id IN ({marks})",  # noqa: S608
            list(items),
        )
    }
    return items, first


def build_inputs(
    repo: RunHistoryRepository,
    items: dict[str, list[str]],
    first: dict[str, str],
    counts: dict[tuple[str, str], int],
) -> job.MergeInputs:
    aids = [a for v in items.values() for a in v]
    members = _load_members(repo, aids, counts)
    entities = {
        a: e
        for a, e in build_join_entities(edge_inputs(members), counts).items()
        if not (a in members and members[a].is_roundup)
    }
    vectors: dict[str, np.ndarray] = {}
    for aid, v in _load_vectors(repo, list(members)).items():
        n = float(np.linalg.norm(v))
        if n:
            vectors[aid] = v / n
    return job.MergeInputs(
        item_of={a: i for i, ids in items.items() for a in ids},
        first_seen=first,
        members=members,
        entities=entities,
        vectors=vectors,
        summary_vectors=repo.load_summary_embeddings(list(members)),
        records={},
    )


def merged_together(
    inputs: job.MergeInputs,
    model: pair_model.PairModel,
    pair: tuple[str, str],
    *,
    real_kind: bool,
    hub_gate: bool,
) -> bool:
    strong: set[tuple[str, str]] = set()
    approved = job.approve_pairs(inputs, model, single_edge_ok=strong, real_kind=real_kind)
    groups = plan_merges(
        item_of=inputs.item_of,
        first_seen=inputs.first_seen,
        entities=inputs.entities,
        vectors=inputs.vectors,
        approved=approved,
        single_edge_ok=strong if job.singleton_rescue_enabled() else None,
        cve_disjoint_gate=True,
        bulletin_bridge_gate=True,
        hub_gate=hub_gate,
        report_periods=job._report_periods(inputs),  # noqa: SLF001
    )
    x, t = pair
    return any(x in (*g.absorbed, g.target) and t in (*g.absorbed, g.target) for g in groups)


@dataclass
class Tally:
    wrong: int = 0  # label=different なのに統合が成立 (誤統合)
    right: int = 0  # label=same で統合が成立 (正しい統合)
    boundary: int = 0
    total_same: int = 0
    total_different: int = 0
    total_boundary: int = 0


def summarize(results: list[dict[str, Any]]) -> dict[tuple[bool, bool], Tally]:
    out: dict[tuple[bool, bool], Tally] = {v: Tally() for v in VARIANTS}
    for r in results:
        label = r["label"]
        for v in VARIANTS:
            tally = out[v]
            merged = r["merged"][f"{int(v[0])}{int(v[1])}"]
            if label == "same":
                tally.total_same += 1
                tally.right += merged
            elif label == "different":
                tally.total_different += 1
                tally.wrong += merged
            else:
                tally.total_boundary += 1
                tally.boundary += merged
    return out


def print_table(summary: dict[tuple[bool, bool], Tally], coverage: tuple[int, int]) -> None:
    print(
        "\n| 種別 | ハブ関門 | 誤統合 (different) | 正しい統合 (same) | 境界 | 適合率(境界除く) |"
    )
    print("|---|---|---|---|---|---|")
    for (real_kind, hub), t in summary.items():
        merged = t.wrong + t.right
        prec = f"{t.right / merged:.1%}" if merged else "-"
        print(
            f"| {'実値' if real_kind else 'other固定'} | {'on' if hub else 'off'} |"
            f" {t.wrong}/{t.total_different} | {t.right}/{t.total_same} |"
            f" {t.boundary}/{t.total_boundary} | {prec} ({t.right}/{merged}) |"
        )
    have, total = coverage
    print(f"\n要約埋込の被覆 (再現した記事): {have}/{total} = {have / max(total, 1):.0%}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", nargs="+", required=True, help="absorbed/target/label の jsonl")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    rows: list[dict[str, Any]] = []
    for path in args.labels:
        rows.extend(
            json.loads(ln)
            for ln in Path(path).read_text(encoding="utf-8").splitlines()
            if ln.strip()
        )
    model = pair_model.load_model()
    if model is None:
        sys.exit("pair_model が読めません")

    repo = RunHistoryRepository()
    counts = _entity_counts(repo, datetime.now(UTC) - timedelta(hours=ENTITY_FREQ_WINDOW_HOURS))
    results: list[dict[str, Any]] = []
    have = total = 0
    for i, row in enumerate(rows):
        items, first = run_group(repo, row)
        inputs = build_inputs(repo, items, first, counts)
        pair = (row["absorbed"], row["target"])
        merged = {
            f"{int(rk)}{int(hub)}": merged_together(inputs, model, pair, real_kind=rk, hub_gate=hub)
            for rk, hub in VARIANTS
        }
        n_have = sum(1 for a in inputs.members if a in inputs.summary_vectors)
        have, total = have + n_have, total + len(inputs.members)
        results.append({**row, "merged": merged, "members": len(inputs.members)})
        if i % 10 == 0:
            print(f"{i}/{len(rows)}", file=sys.stderr)
    Path(args.out).write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in results) + "\n", encoding="utf-8"
    )
    print_table(summarize(results), (have, total))


if __name__ == "__main__":
    main()
