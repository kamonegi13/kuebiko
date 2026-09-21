"""既にできた事象どうしを統合し、統合先の本文を作り直す (手動の一括実行)。

毎時の段 ``eventnews-merge`` (``src/ui/services/eventnews_merge_job.py``) と
**同じ関数** を呼ぶ。判定は ML のみ (LLM は呼ばない)、全事象を対象にする
(旧版は単独メンバーの事象しか見ず、大群どうしは永久に統合されなかった)。

⚠ 統合先は ``current_version`` を 0 に戻す = **本文が消える**。``--apply`` は続けて
再生成まで行う (2026-09-02: 統合だけ適用して 45 件を本文なしにした実例)。
``--regen-cap`` で 1 回の再生成数を抑えると、残りは毎時の段が新しい事象から順に埋める。

使い方:
    python scripts/retro_merge_events.py            # dry-run (計画を表示)
    python scripts/retro_merge_events.py --apply [--regen-cap 40] [--sleep 3]
"""

import argparse
import asyncio
import sys
import time

sys.path.insert(0, "/app")

from src.storage.run_history import RunHistoryRepository
from src.ui.services.eventnews_merge_job import MergePlan, merge_and_regenerate


def _print_plan(plan: MergePlan) -> None:
    if plan.skipped:
        print(f"⚠ 統合しない: {plan.skipped}", flush=True)
        return
    assert plan.inputs is not None
    print(
        f"事象 {len(plan.inputs.records)} / 採点した対 {plan.pairs_scored} / "
        f"ML 承認 {plan.pairs_approved} → 統合する群 {len(plan.groups)} / "
        f"畳む事象 {sum(len(g.absorbed) for g in plan.groups)}",
        flush=True,
    )
    for g in plan.groups:
        # 事象の見出しは版 (本文) 側にあるので、先頭メンバー記事の見出しで代用する
        titles = " / ".join(
            plan.inputs.members[plan.inputs.records[i].state.member_ids[0]].title[:30]
            for i in (g.target, *g.absorbed)
        )
        print(f"  {g.target} ← {len(g.absorbed)} 件 : {titles}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="書き込み + 本文の再生成")
    ap.add_argument("--regen-cap", type=int, default=None, help="再生成の上限 (既定 全件)")
    ap.add_argument("--sleep", type=float, default=3.0, help="生成 1 件ごとの待機秒")
    args = ap.parse_args()

    last = time.monotonic()

    def _progress(i: int, total: int, item_id: str) -> None:
        nonlocal last
        now = time.monotonic()
        print(f"  [{i}/{total}] {item_id} (前件 {now - last:.0f}s)", flush=True)
        last = now
        if args.sleep > 0:
            time.sleep(args.sleep)

    outcome = asyncio.run(
        merge_and_regenerate(
            RunHistoryRepository(),
            apply=args.apply,
            regen_limit=args.regen_cap,
            on_regen_progress=_progress,
        )
    )
    _print_plan(outcome.plan)
    if not args.apply:
        print("\n書き込むには --apply を付ける", flush=True)
        return
    print(
        f"\n適用: {outcome.applied_groups} 群 / 吸収 {outcome.absorbed_items} 事象 / "
        f"再生成 {outcome.regenerated} 件 (残り {outcome.regen_pending}) / "
        f"{outcome.elapsed_seconds:.0f}s",
        flush=True,
    )


main()
