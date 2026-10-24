"""焦点の照合で止めた統合の組から週次に抜き、Opus 裁定に回す入力を作る (F2 の継続測定、2026-10-24)。

docs/eventnews_focus_redesign.md §9.4。止めた組 (event_merge_vetoes の action=blocked、
shadow 中は action=shadow) のうち、まだ抜いていないものから無作為に ``--count`` 組 (既定 10)
を選び、
両側の代表見出し・焦点の名前・止めた理由を jsonl に出す。裁定 (Opus、同一 / 系統 / 別 / unclear の
2 回独立) は別の道具 (data/mlx/adjudication_cache.py) で行い、その結果を ``--summarize`` に渡すと
累計の「本当に別 (different + lineage)」の割合と Wilson 区間を出し、下限が 80% を割れば
shadow に戻すよう促す。

既定は **dry-run** (本番 DB は SELECT のみ。接続は read-only)。``--apply`` で抜いた組に
``sampled_at`` を付ける (= 次回の抽出から外れる)。Ollama・Opus は呼ばない。

使い方:
  set -a; . ./.env; set +a
  DATABASE_URL="postgresql://kuebiko:${POSTGRES_PASSWORD}@127.0.0.1:5433/kuebiko" \\
  uv run python scripts/sample_focus_vetoes.py --out /tmp/vetoes_week.jsonl [--apply]
  uv run python scripts/sample_focus_vetoes.py --summarize labels_w1.jsonl labels_w2.jsonl
    (labels の各行: {"item_a": ..., "item_b": ..., "label": "same|lineage|different|unclear"})
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("EVENTNEWS_ORG_NORM", "1")
os.environ.setdefault("EVENTNEWS_MASKED_VICTIM_GATE", "1")
_APPLY = "--apply" in sys.argv
if not _APPLY:
    os.environ.setdefault("PGOPTIONS", "-c default_transaction_read_only=on")

import src.storage.db_backend as _db  # noqa: E402

if not _APPLY:
    _db.ensure_pg_schema = lambda *a, **k: None  # DDL を流さない

from src.eventnews.identity.focus import focus_to_dict  # noqa: E402
from src.eventnews.identity.replay import (  # noqa: E402
    GATE_REVERT_LOWER_BOUND,
    pick_sample,
    should_revert_to_shadow,
    wilson_interval,
)
from src.eventnews.models import ENTITY_FREQ_WINDOW_HOURS  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.ui.services.eventnews_focus_job import focus_of_members, order_members  # noqa: E402
from src.ui.services.eventnews_hourly_job import _entity_counts, _load_members  # noqa: E402

DEFAULT_COUNT = 10
_TITLES_PER_ITEM = 5
#: 裁定で「本当に別」とみなすラベル (different + lineage)。same は誤って止めた、unclear は除外
TRULY_DIFFERENT = frozenset({"different", "lineage"})
SAMPLED_ACTIONS = ("blocked", "shadow")


def _week_seed(now: datetime) -> int:
    iso = now.isocalendar()
    return iso.year * 100 + iso.week


def _describe_item(
    repo: RunHistoryRepository, item_id: str, counts: dict[tuple[str, str], int]
) -> dict[str, Any]:
    rows = repo.list_members_in_join_order([item_id]).get(item_id, [])
    by_id = _load_members(repo, [a for a, _ in rows], counts)
    members = order_members(rows, by_id)
    focus = focus_of_members(members)
    return {
        "item_id": item_id,
        "article_count": len(members),
        "titles": [m.title for m in members[:_TITLES_PER_ITEM]],
        "focus": focus_to_dict(focus),
    }


def sample(args: argparse.Namespace) -> int:
    repo = RunHistoryRepository()
    now = datetime.now(UTC)
    rows = repo.list_merge_vetoes(
        since=now - timedelta(days=args.days), actions=SAMPLED_ACTIONS, unsampled_only=True
    )
    by_key = {(r.item_a, r.item_b): r for r in rows}
    picked = pick_sample(list(by_key), args.count, seed=args.seed or _week_seed(now))
    print(f"(止めた組 未抽出 {len(rows)} 組から {len(picked)} 組を抜く)", file=sys.stderr)
    counts = _entity_counts(repo, now - timedelta(hours=ENTITY_FREQ_WINDOW_HOURS))
    out: list[str] = []
    for key in picked:
        row = by_key[key]
        out.append(
            json.dumps(
                {
                    "item_a": row.item_a,
                    "item_b": row.item_b,
                    "action": row.action,
                    "klass": row.klass,
                    "detail": row.detail,
                    "edge_count": row.edge_count,
                    "times_seen": row.times_seen,
                    "a": _describe_item(repo, row.item_a, counts),
                    "b": _describe_item(repo, row.item_b, counts),
                },
                ensure_ascii=False,
            )
        )
    text = "\n".join(out) + ("\n" if out else "")
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"{len(out)} 組を {args.out} に書いた", file=sys.stderr)
    else:
        sys.stdout.write(text)
    if args.apply:
        marked = repo.mark_merge_vetoes_sampled(picked, sampled_at=now)
        print(f"抽出済みの印: {marked} 組", file=sys.stderr)
    else:
        print("(dry-run: 抽出済みの印は付けていない。--apply で付ける)", file=sys.stderr)
    return 0


def summarize(paths: list[Path]) -> int:
    seen: dict[tuple[str, str], str] = {}
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            a, b = sorted((str(row["item_a"]), str(row["item_b"])))
            seen[(a, b)] = str(row["label"])
    judged = {k: v for k, v in seen.items() if v != "unclear"}
    different = sum(1 for v in judged.values() if v in TRULY_DIFFERENT)
    total = len(judged)
    low, high = wilson_interval(different, total)
    print(f"裁定 {len(seen)} 組 (unclear {len(seen) - total} を除く {total} 組)")
    if total:
        print(
            f"本当に別 (different + lineage) {different}/{total} = {different / total:.0%}"
            f" (Wilson 95%: {low:.0%}〜{high:.0%})"
        )
    if should_revert_to_shadow(different, total):
        print(
            f"=> 区間の下限が {GATE_REVERT_LOWER_BOUND:.0%} を割っている。"
            " EVENTNEWS_MERGE_FOCUS_GATE=shadow に戻す"
            " (標本が少ないうちは追加の裁定を待つ判断もある)"
        )
    else:
        print(f"=> 区間の下限が {GATE_REVERT_LOWER_BOUND:.0%} 以上。on を続ける")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--days", type=int, default=14, help="何日前までに見た組から抜くか")
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--seed", type=int, default=0, help="0 = 週番号 (同じ週は同じ抽出)")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--apply", action="store_true", help="抜いた組に sampled_at を付ける")
    parser.add_argument("--summarize", nargs="+", type=Path, metavar="LABELS")
    args = parser.parse_args()
    if args.summarize:
        return summarize(args.summarize)
    return sample(args)


if __name__ == "__main__":
    raise SystemExit(main())
