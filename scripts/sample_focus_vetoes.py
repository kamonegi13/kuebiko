"""焦点の照合で止めた統合の組から週次に抜き、Opus 裁定に回す入力を作る (F2 の継続測定、2026-10-24)。

docs/eventnews_focus_redesign.md §9.4。止めた組 (event_merge_vetoes の action=blocked・
hub_split_blocked、shadow 中は action=shadow) のうち、まだ抜いていないものから
無作為に ``--count`` 組 (既定 10) を選び、
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
  (11-20 から抜き出しは週次ジョブ weekly-veto-sample が data/eval/veto_samples/<週>.jsonl に書く。
   裁定の結果は data/eval/veto_samples/labels/<週>.jsonl に置くと、ハートビートの未裁定から外れる)
    (labels の各行: {"item_a": ..., "item_b": ..., "label": "same|lineage|different|unclear"})
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

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

from src.eventnews.identity.replay import (  # noqa: E402
    GATE_REVERT_LOWER_BOUND,
    should_revert_to_shadow,
    wilson_interval,
)
from src.eventnews.veto_sampling import DEFAULT_COUNT, build_sample  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402

#: 裁定で「本当に別」とみなすラベル (different + lineage)。same は誤って止めた、unclear は除外
TRULY_DIFFERENT = frozenset({"different", "lineage"})


def sample(args: argparse.Namespace) -> int:
    repo = RunHistoryRepository()
    now = datetime.now(UTC)
    picked, out, pool = build_sample(
        repo, now=now, count=args.count, days=args.days, seed=args.seed
    )
    print(f"(止めた組 未抽出 {pool} 組から {len(picked)} 組を抜く)", file=sys.stderr)
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
