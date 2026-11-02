#!/usr/bin/env python3
"""S26-IMP-GOLD (2/2): gold:imp_precheck_s26 の正解を Opus 5.5 (effort high・1 回 1 件・2 回独立 + 3 回目) で付ける。

  python scripts/s26_imp/label_imp_gold.py [--dry-run]

prereg §2.2: 呼び出しの順は乱数種 20261102 / 20261103。high / 非 high が割れたら 3 回目で多数決、
2 回で割れた記事は boundary=true。--dry-run は Opus を呼ばず、組み立てた最初の入力の sha と呼び出し回数の計画を出す。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.replay._common import adjudication_lock, assert_no_body  # noqa: E402
from scripts.s26_imp.common import (  # noqa: E402
    DATA_ROOT,
    EFFORT_GOLD,
    LOCK_REL,
    RUBRIC_ID,
    SEED_GOLD,
    SEED_GOLD_2,
    BatchVerdict,
    batch_inputs,
    build_instruction,
    fetch_latest,
    instruction_sha,
    is_roundup_row,
    load_importance_section,
    make_item,
    open_db,
    question_for_pass,
    read_jsonl,
    run_gate,
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
from scripts.s26_imp.protocol import (  # noqa: E402
    gold_vote,
    high_split,
    needs_third,
    representative,
    shuffled,
)

OUT = DATA_ROOT / "data/eval/imp_precheck_s26"
EXPECTED_THIRD_SHARE = 0.08  # prereg §9 の見積もり


def load_items(c: Any) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    sample = read_jsonl(OUT / "sample.jsonl")
    rows = fetch_latest(c, [s["article_id"] for s in sample])
    items: list[dict[str, Any]] = []
    meta: dict[str, dict[str, Any]] = {}
    for s in sample:
        r = rows[s["article_id"]]
        if not verify_body_sha(r, s["body_sha256"]):
            raise RuntimeError(f"body_sha256 不一致: {s['article_id']}")
        items.append(
            make_item(
                s["article_id"],
                (r["title"] or "").strip(),
                (r["feed_title"] or "").strip(),
                r["body"],
            )
        )
        meta[s["article_id"]] = {
            "title": (r["title"] or "").strip(),
            "original_title": (r["original_title"] or "").strip(),
            "stored_importance": s.get("stored_importance"),
        }
    return items, meta


async def run_labeling(
    items: list[dict[str, Any]], judge: Any
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    by_id = {it["id"]: it for it in items}
    js: dict[str, list[dict[str, Any]]] = {}
    failed: list[str] = []
    for pass_no, seed in ((1, SEED_GOLD), (2, SEED_GOLD_2)):
        groups = [[it] for it in shuffled(items, seed)]
        got, bad = await judge_items(groups, pass_no=pass_no, judge=judge)
        failed += bad
        for i, j in got.items():
            js.setdefault(i, []).append(j)
    third_ids = [i for i, v in js.items() if len(v) == 2 and needs_third(v, any_disagreement=False)]
    if third_ids:
        groups = [
            [by_id[i]]
            for i in [x["id"] for x in shuffled([by_id[i] for i in third_ids], SEED_GOLD)]
        ]
        got, bad = await judge_items(groups, pass_no=3, judge=judge)
        failed += bad
        for i, j in got.items():
            js[i].append(j)
    return js, sorted(set(failed))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    rec = run_gate(Path(__file__), dry_run=args.dry_run)
    manifest_path = OUT / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    instruction = build_instruction(load_importance_section())
    ish = instruction_sha(instruction)
    c = open_db()
    items, meta = load_items(c)
    budget = OpusBudget("GOLD", ESTIMATES["GOLD"])

    if args.dry_run:
        first = batch_inputs([shuffled(items, SEED_GOLD)[0]])[0]
        prompt_sha = sha256_text(json.dumps(first, ensure_ascii=False, sort_keys=True))
        groups = [[it] for it in items]
        _, miss = plan_calls(
            groups, pass_no=1, schema=BatchVerdict, instruction=instruction, effort=EFFORT_GOLD
        )
        planned = len(items) * 2 + round(len(items) * EXPECTED_THIRD_SHARE)
        print(
            json.dumps(
                {
                    "items": len(items),
                    "instruction_sha256": ish,
                    "first_input_sha256": prompt_sha,
                    "question_pass1": question_for_pass(1),
                    "planned_opus_calls": planned,
                    "pass1_uncached": miss,
                    "budget_limit": budget.limit,
                },
                ensure_ascii=False,
                indent=1,
            )
        )
        print("[dry-run] Opus は呼ばない")
        return 0

    assert rec is not None
    judge = make_opus_judge(BatchVerdict, instruction, effort=EFFORT_GOLD, budget=budget)
    try:
        with adjudication_lock(DATA_ROOT / LOCK_REL, owner="s26_imp_gold"):
            js, failed = asyncio.run(run_labeling(items, judge))
    except OpusStopError as exc:
        print(f"停止: {exc}")
        return 2
    rows = []
    for it in items:
        v = js.get(it["id"])
        if not v or it["id"] in failed:
            continue
        vote = gold_vote(v)
        rep = representative(v, vote.label)
        m = meta[it["id"]]
        rows.append(
            {
                "article_id": it["id"],
                "title": m["title"],
                "stored_importance": m["stored_importance"],
                "importance": vote.label,
                "boundary": vote.boundary,
                "roundup": is_roundup_row(m["title"], m["original_title"]),
                "criterion": rep["criterion"],
                "judgments": v,
                "n_judgments": len(v),
                "first_second_split": len(v) >= 2 and high_split(v[0], v[1]),
            }
        )
    gold_sha = write_jsonl(OUT / "gold.jsonl", rows)
    manifest["gold"] = {
        "rows": len(rows),
        "failed": failed,
        "opus_calls_charged": budget.used,
        "rubric_id": RUBRIC_ID,
        "instruction_sha256": ish,
        "effort": EFFORT_GOLD,
        "seeds": [SEED_GOLD, SEED_GOLD_2],
        "boundary": sum(r["boundary"] for r in rows),
        "roundup": sum(r["roundup"] for r in rows),
    }
    manifest["files"]["data/eval/imp_precheck_s26/gold.jsonl"] = gold_sha
    assert_no_body(manifest)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + "\n", "utf-8"
    )
    append_ledger("S26-IMP-GOLD", RUBRIC_ID, budget, n_done=len(rows), n_failed=len(failed))
    print(json.dumps(manifest["gold"], ensure_ascii=False))
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
