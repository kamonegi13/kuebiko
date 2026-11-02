#!/usr/bin/env python3
"""S26-IMP-RP9-GOLD: gold:rp9_imp_s26 を作る (prereg §2.3)。rp9_frozen の記事 150 件を Opus 5.5 (effort high・1 回 1 件) で付ける。

  python scripts/s26_imp/label_rp9_imp.py [--dry-run]

本文は DB から article_id で読み、凍結時の body_sha256 と違う記事は外して数える。出力に「境界か」
(判定役の自己申告。合否の枠から外す) の欄を足す。s26・s24ci65 の出力を見る前に作り、sha256 を固定する。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.replay._common import adjudication_lock, assert_no_body  # noqa: E402
from scripts.s26_imp.common import (  # noqa: E402
    DATA_ROOT,
    EFFORT_GOLD,
    LOCK_REL,
    PREREG,
    RUBRIC_ID,
    SEED_GOLD,
    BatchVerdictBoundary,
    batch_inputs,
    build_instruction,
    fetch_latest,
    git_head,
    instruction_sha,
    is_roundup_row,
    load_importance_section,
    make_item,
    open_db,
    read_jsonl,
    run_gate,
    sha256_bytes,
    sha256_text,
    verify_body_sha,
    write_jsonl,
)
from scripts.s26_imp.opus import (  # noqa: E402
    ESTIMATES,
    OpusBudget,
    OpusStopError,
    append_ledger,
    judge_items,
    make_opus_judge,
    plan_calls,
)
from scripts.s26_imp.protocol import shuffled  # noqa: E402

FROZEN = DATA_ROOT / "data/eval/rp9_frozen/frozen_articles.jsonl"
OUT = DATA_ROOT / "data/eval/rp9_imp_s26"


def main() -> int:  # noqa: PLR0915
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    rec = run_gate(Path(__file__), dry_run=args.dry_run, out_dir=None if args.dry_run else OUT)
    frozen = read_jsonl(FROZEN)
    c = open_db()
    rows = fetch_latest(c, [f["article_id"] for f in frozen])
    items: list[dict[str, Any]] = []
    meta: dict[str, dict[str, Any]] = {}
    mismatched: list[str] = []
    for f in frozen:
        r = rows.get(f["article_id"])
        if r is None or not verify_body_sha(r, f["body_sha256"]):
            mismatched.append(f["article_id"])
            continue
        items.append(
            make_item(
                f["article_id"],
                (r["title"] or "").strip(),
                (r["feed_title"] or "").strip(),
                r["body"],
            )
        )
        meta[f["article_id"]] = {
            "stratum": f["stratum"],
            "weight": f["weight"],
            "title": (r["title"] or "").strip(),
            "original_title": (r["original_title"] or "").strip(),
            "importance_at_freeze": f.get("importance_at_freeze"),
        }
    instruction = build_instruction(load_importance_section(), with_boundary=True)
    ish = instruction_sha(instruction)
    budget = OpusBudget("RP9-GOLD", ESTIMATES["RP9-GOLD"])
    groups = [[it] for it in shuffled(items, SEED_GOLD)]

    if args.dry_run:
        _, miss = plan_calls(
            groups,
            pass_no=1,
            schema=BatchVerdictBoundary,
            instruction=instruction,
            effort=EFFORT_GOLD,
        )
        first = batch_inputs(groups[0])[0]
        print(
            json.dumps(
                {
                    "frozen": len(frozen),
                    "usable": len(items),
                    "body_sha_mismatch_or_missing": len(mismatched),
                    "instruction_sha256": ish,
                    "first_input_sha256": sha256_text(
                        json.dumps(first, ensure_ascii=False, sort_keys=True)
                    ),
                    "planned_opus_calls": len(items),
                    "uncached": miss,
                    "budget_limit": budget.limit,
                },
                ensure_ascii=False,
                indent=1,
            )
        )
        print("[dry-run] Opus は呼ばない")
        return 0

    assert rec is not None
    judge = make_opus_judge(BatchVerdictBoundary, instruction, effort=EFFORT_GOLD, budget=budget)
    try:
        with adjudication_lock(DATA_ROOT / LOCK_REL, owner="s26_imp_rp9"):
            got, failed = asyncio.run(judge_items(groups, pass_no=1, judge=judge))
    except OpusStopError as exc:
        print(f"停止: {exc}")
        return 2
    out = []
    for it in items:
        v = got.get(it["id"])
        if v is None:
            continue
        m = meta[it["id"]]
        out.append(
            {
                "article_id": it["id"],
                "stratum": m["stratum"],
                "weight": m["weight"],
                "title": m["title"],
                "importance": v["importance"],
                "criterion": v["criterion"],
                "reason": v["reason"],
                "confirmed_victim_orgs": v["confirmed_victim_orgs"],
                "confirmed_by": v["confirmed_by"],
                "article_form": v["article_form"],
                "boundary_self_report": v["boundary"],
                "roundup": is_roundup_row(m["title"], m["original_title"]),
                "importance_at_freeze": m["importance_at_freeze"],
            }
        )
    gold_sha = write_jsonl(OUT / "gold.jsonl", out)
    manifest = {
        "name": "rp9_imp_s26",
        "item": "S26-IMP-RP9-GOLD",
        "built_at": datetime.now(UTC).isoformat(),
        "prereg": PREREG,
        "prereg_git_sha": rec.git_sha,
        "prereg_blob_sha256": rec.blob_sha256,
        "head_sha": rec.head_sha,
        "git_head": git_head(),
        "rubric_id": RUBRIC_ID,
        "instruction_sha256": ish,
        "effort": EFFORT_GOLD,
        "frozen_articles_sha256": sha256_bytes(FROZEN.read_bytes()),
        "counts": {
            "frozen": len(frozen),
            "body_sha_mismatch_or_missing": len(mismatched),
            "judged": len(out),
            "failed": len(failed),
            "high": sum(r["importance"] == "high" for r in out),
            "boundary_self_report": sum(r["boundary_self_report"] for r in out),
            "roundup": sum(r["roundup"] for r in out),
        },
        "excluded_article_ids": mismatched,
        "opus_calls_charged": budget.used,
        "files": {"data/eval/rp9_imp_s26/gold.jsonl": gold_sha},
    }
    assert_no_body(manifest)
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + "\n", "utf-8"
    )
    append_ledger("S26-IMP-RP9-GOLD", RUBRIC_ID, budget, n_done=len(out), n_failed=len(failed))
    print(json.dumps(manifest["counts"], ensure_ascii=False))
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
