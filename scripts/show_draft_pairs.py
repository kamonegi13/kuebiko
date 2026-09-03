"""草稿の選好対 (関門に落ちた版 → 通った版) を読む / 学習用に書き出す。

⚠ event_draft_rejects の**消費者** (CLAUDE.md §7 — write-only テーブルを作らない)。
採用側とプロンプトは event_item_versions が持つので、ここで結合して対にする。

使い方:
    python scripts/show_draft_pairs.py                # 集計 + 直近の例
    python scripts/show_draft_pairs.py --export out.jsonl   # DPO 用 (prompt/chosen/rejected)
"""

import argparse
import json
import sys

sys.path.insert(0, "/app")

from src.storage.run_history import RunHistoryRepository


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--export", default="", help="JSONL の書き出し先")
    args = ap.parse_args()

    repo = RunHistoryRepository()
    rejects = repo.list_draft_rejects(limit=args.limit)
    print(f"棄却草稿 {len(rejects)} 件")
    pairs = []
    for r in rejects:
        versions = {v.version: v for v in repo.list_event_versions(str(r["item_id"]))}
        v = versions.get(int(str(r["version"])))
        prompt = repo.get_version_prompt(str(r["item_id"]), int(str(r["version"])))
        if v is None or not prompt or not v.body_json:
            continue
        pairs.append(
            {
                "item_id": r["item_id"],
                "version": r["version"],
                "model": r["model"],
                "hints": r["hints"],
                "prompt": prompt,
                "chosen": v.body_json,
                "rejected": r["draft_json"],
            }
        )
    print(f"対にできた {len(pairs)} 件 (採用版とプロンプトが揃っているもの)")
    for p in pairs[:3]:
        print(f"  {p['item_id']} v{p['version']} 理由: {str(p['hints'])[:60]}")
    if args.export:
        with open(args.export, "w", encoding="utf-8") as f:
            for p in pairs:
                f.write(json.dumps(p, ensure_ascii=False) + "\n")
        print(f"書き出し: {args.export}")


if __name__ == "__main__":
    main()
