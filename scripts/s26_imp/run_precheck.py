#!/usr/bin/env python3
"""S26-IMP-PRECHECK: 教師の手順を gold:imp_precheck_s26 に当てて §6-A の A1/A2/A3 を測る。

  python scripts/s26_imp/run_precheck.py [--dry-run]

教師の手順 = 4 件ずつ・effort medium・乱数種 20261104/05/06・§3.1 の 2 回目 + 多数決。
枠はまとめ記事 (見出しの規則) と正解の境界を除く。1 つでも外れたら止める (付け直しに進まない)。
この集合で指示・線を変えて測り直さない。
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

from scripts.replay._common import adjudication_lock  # noqa: E402
from scripts.s26_imp.common import (  # noqa: E402
    DATA_ROOT,
    EFFORT_TEACHER,
    LOCK_REL,
    RUBRIC_ID,
    SEED_TEACHER,
    BatchVerdict,
    batch_items,
    build_instruction,
    instruction_sha,
    load_importance_section,
    open_db,
    read_jsonl,
    run_gate,
    sha256_text,
    write_jsonl,
)
from scripts.s26_imp.label_imp_gold import load_items  # noqa: E402
from scripts.s26_imp.opus import (  # noqa: E402
    ESTIMATES,
    OpusBudget,
    OpusStopError,
    append_ledger,
    make_opus_judge,
    plan_calls,
)
from scripts.s26_imp.protocol import (  # noqa: E402
    PrecheckItem,
    precheck_metrics,
    run_teacher_protocol,
    shuffled,
)

GOLD_DIR = DATA_ROOT / "data/eval/imp_precheck_s26"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    rec = run_gate(Path(__file__), dry_run=args.dry_run)
    instruction = build_instruction(load_importance_section())
    ish = instruction_sha(instruction)
    c = open_db()
    items, _meta = load_items(c)
    budget = OpusBudget("PRECHECK", ESTIMATES["PRECHECK"])
    groups = batch_items(shuffled(items, SEED_TEACHER[0]))

    if args.dry_run:
        n_groups, miss = plan_calls(
            groups, pass_no=1, schema=BatchVerdict, instruction=instruction, effort=EFFORT_TEACHER
        )
        first = json.dumps(groups[0], ensure_ascii=False, sort_keys=True) if groups else ""
        print(
            json.dumps(
                {
                    "items": len(items),
                    "instruction_sha256": ish,
                    "first_group_sha256": sha256_text(first),
                    "pass1_calls": n_groups,
                    "pass1_uncached": miss,
                    "planned_opus_calls_total": "約 52 (1 回目 + 約 30% の 2 回目 + 3 回目)",
                    "budget_limit": budget.limit,
                },
                ensure_ascii=False,
                indent=1,
            )
        )
        print("[dry-run] Opus は呼ばない")
        return 0

    assert rec is not None
    if (GOLD_DIR / "precheck_result.json").exists():
        print("precheck_result.json が既にある。この集合で測り直さない (prereg §6-A)。止める")
        return 2
    gold = {g["article_id"]: g for g in read_jsonl(GOLD_DIR / "gold.jsonl")}
    use = [it for it in items if it["id"] in gold]
    judge = make_opus_judge(BatchVerdict, instruction, effort=EFFORT_TEACHER, budget=budget)
    try:
        with adjudication_lock(DATA_ROOT / LOCK_REL, owner="s26_imp_precheck"):
            res = asyncio.run(run_teacher_protocol(use, judge))
    except OpusStopError as exc:
        print(f"停止: {exc}")
        return 2
    pitems: list[PrecheckItem] = [
        PrecheckItem(
            id=i,
            gold_label=gold[i]["importance"],
            gold_boundary=bool(gold[i]["boundary"]),
            roundup=bool(gold[i]["roundup"]),
            teacher=row,
        )
        for i, row in res.rows.items()
    ]
    metrics: dict[str, Any] = precheck_metrics(pitems)
    result = {
        "item": "S26-IMP-PRECHECK",
        "run_at": datetime.now(UTC).isoformat(),
        "prereg_git_sha": rec.git_sha,
        "rubric_id": RUBRIC_ID,
        "instruction_sha256": ish,
        "effort": EFFORT_TEACHER,
        "seeds": list(SEED_TEACHER),
        "failed_items": res.failed,
        "pass_sizes": res.pass_sizes,
        "pass_groups": res.pass_groups,
        "opus_calls_charged": budget.used,
        "metrics": metrics,
    }
    rows_sha = write_jsonl(
        GOLD_DIR / "precheck_teacher.jsonl",
        (
            {
                "article_id": r.id,
                "label": r.label,
                "boundary": r.boundary,
                "flags_first": list(r.flags_first),
                "flags_final": list(r.flags_final),
                "judgments": list(r.judgments),
            }
            for r in res.rows.values()
        ),
    )
    result["teacher_rows_sha256"] = rows_sha
    (GOLD_DIR / "precheck_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True) + "\n", "utf-8"
    )
    append_ledger(
        "S26-IMP-PRECHECK", RUBRIC_ID, budget, n_done=len(res.rows), n_failed=len(res.failed)
    )
    print(json.dumps(metrics["values"], ensure_ascii=False), metrics["frame"])
    print(metrics["verdict"])
    return 0 if metrics["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
