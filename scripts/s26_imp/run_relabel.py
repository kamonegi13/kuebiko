#!/usr/bin/env python3
"""S26-IMP-RELABEL: s26 の要約の教師の行の importance を v8r7 で付け直す (prereg §2.1・§3・§6-B)。

  python scripts/s26_imp/run_relabel.py [--dry-run] [--dataset data/mlx/dataset_s26_dry]

要約の行 (train + valid) は組み立て済みの dataset から読む (組み立ては
`S24_OUT_NAME=s26_dry uv run python data/mlx/build_dataset_s26.py`。実行の直前に組み直して行数と鍵の
sha256 を manifest に残す)。判定役には今の importance を見せない (盲検)。§6-B の関門 B1〜B3 が
全て通ったときだけ付け直しの表 data/mlx/teacher/summary_importance_v8r7_s26.jsonl を書く。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.replay._common import adjudication_lock  # noqa: E402
from scripts.s26_imp.common import (  # noqa: E402
    BODY_MAX,
    DATA_ROOT,
    DATASET_DRY_REL,
    EFFORT_TEACHER,
    LOCK_REL,
    RUBRIC_ID,
    SEED_TEACHER,
    BatchVerdict,
    batch_items,
    build_instruction,
    git_head,
    instruction_sha,
    is_roundup_row,
    load_importance_section,
    read_jsonl,
    run_gate,
    sha256_text,
    write_jsonl,
)
from scripts.s26_imp.opus import (  # noqa: E402
    ESTIMATES,
    OpusBudget,
    OpusStopError,
    append_ledger,
    make_opus_judge,
    plan_calls,
)
from scripts.s26_imp.protocol import (  # noqa: E402
    RelabelRow,
    relabel_gates,
    representative,
    run_teacher_protocol,
    shuffled,
)

EXPECTED_ROWS = 994
TABLE_OUT = DATA_ROOT / "data/mlx/teacher/summary_importance_v8r7_s26.jsonl"
REPORT_DIR = DATA_ROOT / "data/eval/s26_relabel"
_TITLE = re.compile(r"【記事タイトル】\n(.*?)\n\n【記事 ソース】\n(.*?)\n【記事 本文】\n", re.S)
_BODY_END = "\n\n---\n\n# フィールド別の判定基準"
_KEEP = (
    "importance",
    "criterion",
    "reason",
    "confirmed_victim_orgs",
    "confirmed_by",
    "article_form",
)


def summary_rows(dataset: Path) -> list[dict[str, Any]]:
    """dataset の要約の行 (train + valid)。鍵 = source_row_key:body_sha 先頭 12 (build の finalize_summary と同じ)。"""
    out: list[dict[str, Any]] = []
    for split in ("train", "valid"):
        for r in read_jsonl(dataset / f"{split}.jsonl"):
            if "[task: summary]" not in r["messages"][0]["content"]:
                continue
            user = r["messages"][-2]["content"]
            m = _TITLE.search(user)
            if not m:
                raise RuntimeError("見出し・配信元を読めない行")
            meta = r["meta"]
            comp = json.loads(r["messages"][-1]["content"])
            out.append(
                {
                    "key": f"{meta['source_row_key']}:{meta['body_sha'][:12]}",
                    "split": split,
                    "origin": meta["origin"],
                    "category": comp.get("category"),
                    "old": comp["importance"],
                    "title": m.group(1),
                    "feed": m.group(2),
                    "body": user[m.end() : user.rindex(_BODY_END)][:BODY_MAX],
                }
            )
    keys = [x["key"] for x in out]
    if len(set(keys)) != len(keys):
        raise RuntimeError("行の鍵が重複")
    return out


def keys_sha256(keys: list[str]) -> str:
    return sha256_text("\n".join(sorted(keys)))


def table_row(row: dict[str, Any], t: Any) -> dict[str, Any]:
    rep = representative(list(t.judgments), t.label)
    return {
        "key": row["key"],
        "split": row["split"],
        "origin": row["origin"],
        "category": row["category"],
        "old": row["old"],
        "new": t.label,
        "criterion": rep["criterion"],
        "reason": rep["reason"],
        "rubric": RUBRIC_ID,
        "judgments": [{k: j.get(k) for k in _KEEP} for j in t.judgments],
        "boundary": t.boundary,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dataset", default=DATASET_DRY_REL)
    args = ap.parse_args()
    rec = run_gate(Path(__file__), dry_run=args.dry_run)
    rows = summary_rows(DATA_ROOT / args.dataset)
    by_key = {r["key"]: r for r in rows}
    items = [
        {"id": r["key"], "title": r["title"], "feed": r["feed"], "body": r["body"]} for r in rows
    ]
    instruction = build_instruction(load_importance_section())
    ish = instruction_sha(instruction)
    budget = OpusBudget("RELABEL", ESTIMATES["RELABEL"])
    info = {
        "rows": len(rows),
        "expected_rows": EXPECTED_ROWS,
        "row_count_differs": len(rows) != EXPECTED_ROWS,
        "key_list_sha256": keys_sha256(list(by_key)),
        "old_importance": {k: sum(r["old"] == k for r in rows) for k in ("high", "medium", "low")},
        "instruction_sha256": ish,
    }

    if args.dry_run:
        groups = batch_items(shuffled(items, SEED_TEACHER[0]))
        n_groups, miss = plan_calls(
            groups, pass_no=1, schema=BatchVerdict, instruction=instruction, effort=EFFORT_TEACHER
        )
        info |= {
            "first_group_sha256": sha256_text(
                json.dumps(groups[0], ensure_ascii=False, sort_keys=True)
            ),
            "pass1_calls": n_groups,
            "pass1_uncached": miss,
            "planned_opus_calls_total": "約 327 (1 回目 + 境の型・検査の 2 回目 + 3 回目)",
            "budget_limit": budget.limit,
        }
        print(json.dumps(info, ensure_ascii=False, indent=1))
        print("[dry-run] Opus は呼ばない・何も書かない")
        return 0

    assert rec is not None
    judge = make_opus_judge(BatchVerdict, instruction, effort=EFFORT_TEACHER, budget=budget)
    try:
        with adjudication_lock(DATA_ROOT / LOCK_REL, owner="s26_imp_relabel"):
            res = asyncio.run(run_teacher_protocol(items, judge))
    except OpusStopError as exc:
        print(f"停止: {exc}")
        return 2
    rrows = [
        RelabelRow(
            key=k,
            old=by_key[k]["old"],
            category=by_key[k]["category"],
            roundup=is_roundup_row(by_key[k]["title"]),
            teacher=t,
        )
        for k, t in res.rows.items()
    ]
    gates = relabel_gates(rrows, res.failed, len(rows))
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    full = [table_row(by_key[r.key], r.teacher) for r in rrows]
    report = {
        "item": "S26-IMP-RELABEL",
        "run_at": datetime.now(UTC).isoformat(),
        "prereg_git_sha": rec.git_sha,
        "git_head": git_head(),
        "info": info,
        "seeds": list(SEED_TEACHER),
        "pass_sizes": res.pass_sizes,
        "pass_groups": res.pass_groups,
        "failed_keys": res.failed,
        "opus_calls_charged": budget.used,
        "gates": gates,
        "table_written": False,
    }
    report["rows_sha256"] = write_jsonl(REPORT_DIR / "relabel_rows.jsonl", full)
    if gates["verdict"] == "PASS":
        report["table_written"] = True
        report["table_sha256"] = write_jsonl(TABLE_OUT, full)
    (REPORT_DIR / "relabel_result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True) + "\n", "utf-8"
    )
    append_ledger("S26-IMP-RELABEL", RUBRIC_ID, budget, n_done=len(full), n_failed=len(res.failed))
    print(json.dumps(gates, ensure_ascii=False, indent=1))
    print(gates["verdict"], "(表を書いた)" if report["table_written"] else "(表は書かない)")
    return 0 if gates["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
