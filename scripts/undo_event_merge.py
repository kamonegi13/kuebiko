"""統合を戻す (誤統合の取り消し)。**既定は dry-run**。

統合は吸収側の事象の行を残すので戻せる。戻す操作の本体は
``src/ui/services/eventnews_merge_undo.undo_merge`` 1 か所 (このスクリプトは呼ぶだけ)。
統合の判断 (辺・確率・設定) は ``event_merge_decisions`` に残っているので、
``--list`` で根拠を見てから戻す対象を決める。

使い方:
    python scripts/undo_event_merge.py --list [--target ev-xxx | --absorbed ev-yyy]
    python scripts/undo_event_merge.py --absorbed ev-yyy [--absorbed ev-zzz ...]    # dry-run
    python scripts/undo_event_merge.py --target ev-xxx --apply --reason "ハブへの吸収"
    python scripts/undo_event_merge.py --absorbed ev-yyy --apply [--no-generate]

⚠ 戻すと統合先の本文が消える (current_version=0)。``--apply`` は続けて再生成まで行う
(``--no-generate`` で外すと毎時の段が新しい事象から順に埋める)。
"""

import argparse
import json
import sys
from datetime import UTC, datetime

sys.path.insert(0, "/app")

from src.storage.repo_merge_decisions import MergeDecisionRow
from src.storage.run_history import RunHistoryRepository
from src.ui.services.eventnews_hourly_job import regenerate_pending_bodies
from src.ui.services.eventnews_merge_undo import STATUS_RESTORED, undo_merge

DEFAULT_REASON = "manual undo"
LIST_LIMIT = 200


def _print_decision(d: MergeDecisionRow) -> None:
    state = f"戻し済 {d.undone_at:%m-%d %H:%M}" if d.undone_at else "有効"
    edges = json.loads(d.edges_json)
    top = ", ".join(
        f"{e['a'][:8]}~{e['b'][:8]} cos={e['cos']} p={e['proba']} {e['source']}" for e in edges[:3]
    )
    print(
        f"  {d.absorbed_id} → {d.target_id} [{d.run_kind}] 辺 {d.edge_count}"
        f"{' (救済)' if d.rescued else ''} {state}\n      {top}\n      設定 {d.flags_json}",
        flush=True,
    )


def _absorbed_targets(repo: RunHistoryRepository, args: argparse.Namespace) -> list[str]:
    ids = list(args.absorbed)
    for target in args.target:
        ids.extend(
            d.absorbed_id for d in repo.list_merge_decisions(target_id=target, active_only=True)
        )
        # 判断記録が無い統合 (記録導入前) は、吸収側の merged_into から探す
        ids.extend(_legacy_absorbed(repo, target))
    return list(dict.fromkeys(ids))


def _legacy_absorbed(repo: RunHistoryRepository, target: str) -> list[str]:
    with repo._connect() as conn:  # noqa: SLF001 — 運用スクリプトの読み取り
        rows = conn.execute(
            "SELECT id FROM event_items WHERE merged_into = ?", (target,)
        ).fetchall()
    return [str(r["id"]) for r in rows]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--absorbed", action="append", default=[], help="戻す吸収側の事象 id")
    ap.add_argument("--target", action="append", default=[], help="この統合先に吸収された全て")
    ap.add_argument("--list", action="store_true", help="判断記録を表示して終わる")
    ap.add_argument("--apply", action="store_true", help="書き込む (既定 dry-run)")
    ap.add_argument("--reason", default=DEFAULT_REASON)
    ap.add_argument("--no-generate", action="store_true")
    ap.add_argument("--sleep", type=float, default=3.0)
    args = ap.parse_args()

    repo = RunHistoryRepository()
    if args.list:
        found = []
        for t in args.target or [None]:
            for a in args.absorbed or [None]:
                found.extend(
                    repo.list_merge_decisions(target_id=t, absorbed_id=a, limit=LIST_LIMIT)
                )
        print(f"判断記録 {len(found)} 件", flush=True)
        for d in found:
            _print_decision(d)
        return

    ids = _absorbed_targets(repo, args)
    if not ids:
        print("対象がありません (--absorbed / --target / --list)", flush=True)
        return
    now = datetime.now(UTC)
    restored = 0
    for aid in ids:
        res = undo_merge(repo, aid, reason=args.reason, now=now, apply=args.apply)
        print(
            f"  {aid}: {res.status} (祖先 {len(res.ancestors)} / 記事 {res.articles}"
            f" / 消す行 {res.removed_rows})",
            flush=True,
        )
        restored += res.status == STATUS_RESTORED
    if not args.apply:
        print("\ndry-run。書き込むには --apply を付ける", flush=True)
        return
    print(f"\n戻した: {restored} 件", flush=True)
    if restored and not args.no_generate:
        regenerate_pending_bodies(repo, args.sleep)


main()
