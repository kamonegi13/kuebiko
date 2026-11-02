"""E1: 既存の対読 (線なし A / 線つき B) を提示順の両方で採点し直し、主張の裏付けを照合する。

    uv run python -m scripts.graphrag_eval.e1_rescore --out data/eval/graphrag_e1_2026_11_02 [--dry-run]

事前登録: docs/replay_prereg/graphrag_e1_rescore.md。新しい生成はしない。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from scripts.graphrag_eval.common import (
    ROOT,
    BudgetExceededError,
    OpusBudget,
    make_judge,
    now_iso,
    require_prereg,
    write_json,
)
from scripts.graphrag_eval.prompts import (
    E1_CLAIM_QUESTION,
    E1_CLAIM_RUBRIC,
    E1_PAIRWISE_QUESTION,
    E1_PAIRWISE_RUBRIC,
    TASK_DESC,
    ClaimVerdict,
    PairwiseVerdict,
)
from scripts.graphrag_eval.stats import (
    ORDERS,
    ClaimTally,
    Order,
    PairwiseRow,
    arm_of_label,
    e1_task_result,
    outputs_in_order,
    unblind,
    win_rate_by_length,
)

PREREG = "graphrag_e1_rescore"
SEED = 20261102
ESTIMATE = 135
MAX_TOKENS_PAIRWISE = 2000
MAX_TOKENS_CLAIMS = 16000

INPUTS: dict[str, tuple[str, str]] = {
    "spotlight": (
        "data/mlx/graphrag_spotlight.jsonl",
        "d2b5d7bd40c986e772ac285e9e1132a9cb9b99d0791fdb0816e71e49b4449b88",
    ),
    "synthesis": (
        "data/eval/graphrag_synthesis.jsonl",
        "657317320a8b99d00f6bf5e7678d39992d4ed4cfc600ec09d9437fb6b58b474b",
    ),
    "eventnews": (
        "data/eval/graphrag_eventnews_opus.jsonl",
        "8cf977babaea50e12cd8391df56a39aa1bbc6fd2ba172a6240582ac7efb3557c",
    ),
}


def split_insertion(prompt_a: str, prompt_b: str) -> str:
    """prompt_b = prompt_a に 1 か所挿入した文字列のとき、挿入された節を返す (そうでなければ例外)。"""
    i = 0
    while i < len(prompt_a) and prompt_a[i] == prompt_b[i]:
        i += 1
    tail = len(prompt_a) - i
    inserted = prompt_b[i : len(prompt_b) - tail]
    if prompt_b[:i] + inserted + prompt_b[len(prompt_b) - tail :] != prompt_b or (
        prompt_b[len(prompt_b) - tail :] != prompt_a[i:]
    ):
        raise ValueError("prompt_b が prompt_a への 1 か所の挿入ではない")
    if not inserted.strip():
        raise ValueError("挿入された節が空")
    return inserted


def claim_orders(
    tasks: list[tuple[str, list[dict[str, Any]]]], seed: int = SEED
) -> dict[str, Order]:
    """主張の照合の X/Y の割り当て (課題 → 行の順に 1 つの乱数列から)。"""
    rng = random.Random(seed)
    out: dict[str, Order] = {}
    for task, rows in tasks:
        for r in rows:
            out[f"{task}:{r['key']}"] = "BA" if rng.random() < 0.5 else "AB"
    return out


def tally_claims(task: str, key: str, v: ClaimVerdict, order: Order) -> list[ClaimTally]:
    out = []
    for label, claims in (("X", v.claims_x), ("Y", v.claims_y)):
        arm = arm_of_label(label, order)  # type: ignore[arg-type]
        ok = ("記事",) if arm == "A" else ("記事", "線の節のみ")
        out.append(
            ClaimTally(
                task=task,
                key=key,
                arm=arm,
                total=len(claims),
                supported=sum(c.support in ok for c in claims),
                misattributed=sum(c.misattributed for c in claims) if arm == "B" else 0,
            )
        )
    return out


def load_inputs() -> list[tuple[str, list[dict[str, Any]]]]:
    tasks = []
    for task, (rel, want) in INPUTS.items():
        path = ROOT / rel
        got = hashlib.sha256(path.read_bytes()).hexdigest()
        if got != want:
            raise RuntimeError(f"{rel} の sha256 が prereg と違う: {got}")
        rows = [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]
        if len(rows) != 15 or any(not r.get("a") or not r.get("b") for r in rows):
            raise RuntimeError(f"{rel} の出力が欠けている")
        tasks.append((task, rows))
    return tasks


async def run(
    tasks: list[tuple[str, list[dict[str, Any]]]], budget: OpusBudget, out: Path
) -> dict[str, Any]:
    pj = make_judge(PairwiseVerdict, E1_PAIRWISE_RUBRIC, budget, max_tokens=MAX_TOKENS_PAIRWISE)
    cj = make_judge(ClaimVerdict, E1_CLAIM_RUBRIC, budget, max_tokens=MAX_TOKENS_CLAIMS)
    corders = claim_orders(tasks)
    pairwise: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    missing: list[str] = []

    async def one_pair(task: str, r: dict[str, Any], order: Order) -> None:
        x, y = outputs_in_order(r["a"], r["b"], order)
        inputs = {"入力": r["prompt_b"], "出力 X": x, "出力 Y": y}
        v = await pj(E1_PAIRWISE_QUESTION.format(task=TASK_DESC[task]), inputs)
        if not isinstance(v, PairwiseVerdict):
            missing.append(f"pairwise:{task}:{r['key']}:{order}")
            return
        pairwise.append(
            {
                "task": task,
                "key": r["key"],
                "order": order,
                "winner": unblind(v.winner, order),
                "grounding": unblind(v.grounding, order),
                "usefulness": unblind(v.usefulness, order),
                "outlook": unblind(v.outlook, order),
                "winner_label": v.winner,
                "len_a": len(r["a"]),
                "len_b": len(r["b"]),
                "reason": v.reason,
            }
        )

    async def one_claim(task: str, r: dict[str, Any]) -> None:
        order = corders[f"{task}:{r['key']}"]
        x, y = outputs_in_order(r["a"], r["b"], order)
        link = split_insertion(r["prompt_a"], r["prompt_b"])
        inputs = {"記事の節": r["prompt_a"], "線の節": link, "出力 X": x, "出力 Y": y}
        v = await cj(E1_CLAIM_QUESTION, inputs)
        if not isinstance(v, ClaimVerdict):
            missing.append(f"claims:{task}:{r['key']}")
            return
        for t in tally_claims(task, r["key"], v, order):
            claims.append({**asdict(t), "order": order})

    jobs = [one_pair(t, r, o) for t, rows in tasks for r in rows for o in ORDERS]
    jobs += [one_claim(t, r) for t, rows in tasks for r in rows]
    stopped = None
    try:
        await asyncio.gather(*jobs)
    except BudgetExceededError as exc:
        stopped = str(exc)
    for name, rows_out in (("pairwise.jsonl", pairwise), ("claims.jsonl", claims)):
        with (out / name).open("w", encoding="utf-8") as fh:
            for row in sorted(
                rows_out, key=lambda z: (z["task"], z["key"], z.get("order", ""), z.get("arm", ""))
            ):
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return {"missing": missing, "stopped": stopped}


def summarize(out: Path) -> dict[str, Any]:
    prow = [json.loads(x) for x in (out / "pairwise.jsonl").read_text("utf-8").splitlines() if x]
    crow = [json.loads(x) for x in (out / "claims.jsonl").read_text("utf-8").splitlines() if x]
    rows = [
        PairwiseRow(r["task"], r["key"], r["order"], r["winner"], r["len_a"], r["len_b"])
        for r in prow
    ]
    tallies = [
        ClaimTally(c["task"], c["key"], c["arm"], c["total"], c["supported"], c["misattributed"])
        for c in crow
    ]
    res: dict[str, Any] = {}
    for task in INPUTS:
        d = asdict(e1_task_result(task, rows, tallies))
        d["length_split"] = win_rate_by_length([r for r in rows if r.task == task])
        d["axes"] = {
            ax: {
                o: _count(r[ax] for r in prow if r["task"] == task and r["order"] == o)
                for o in ORDERS
            }
            for ax in ("grounding", "usefulness", "outlook")
        }
        res[task] = d
    return res


def _count(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


def teacher_build_refs() -> list[str]:
    """教師の組み立て (data/mlx/build_dataset_*.py) が対読のファイルを読んでいないか (prereg §8)。"""
    names = [Path(p).name for p, _ in INPUTS.values()]
    hits = []
    for f in sorted((ROOT / "data" / "mlx").glob("build_dataset_*.py")):
        text = f.read_text("utf-8", errors="replace")
        hits += [f"{f.name}:{n}" for n in names if n in text]
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    out: Path = args.out if args.out.is_absolute() else ROOT / args.out
    gate = require_prereg(PREREG, Path(__file__), out)
    tasks = load_inputs()
    for _task, rows in tasks:
        for r in rows:
            split_insertion(r["prompt_a"], r["prompt_b"])
    budget = OpusBudget("GRAPHRAG-E1", ESTIMATE)
    print(json.dumps(gate, ensure_ascii=False))
    print(f"窓 {sum(len(r) for _, r in tasks)} / 見積もり {budget.estimate} / 上限 {budget.limit}")
    if args.dry_run:
        return 0
    out.mkdir(parents=True, exist_ok=True)
    status = asyncio.run(run(tasks, budget, out))
    manifest = {
        **gate,
        "item": "GRAPHRAG-E1",
        "judge": "claude-opus-5-5",
        "effort": "high",
        "inputs": {t: {"path": p, "sha256": s} for t, (p, s) in INPUTS.items()},
        "teacher_build_refs": teacher_build_refs(),
        "opus_calls_bridge": budget.used,
        "budget_limit": budget.limit,
        **status,
        "finished_at": now_iso(),
    }
    write_json(out / "manifest.json", manifest)
    summary = summarize(out)
    write_json(out / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    print("Opus", budget.used, "欠け", len(status["missing"]), "停止", status["stopped"])
    return 0 if status["stopped"] is None else 2


if __name__ == "__main__":
    sys.exit(main())
