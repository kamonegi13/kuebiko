"""E2: 標本の組を Opus が盲検で判定し、型ごとの精度を集計する (prereg graphrag_e2_edge_precision.md)。

    uv run python -m scripts.graphrag_eval.e2_judge --dir data/eval/graphrag_e2_2026_11_02 [--check-only | --dry-run]

標本は e2_population.py (read-only のコンテナ) が書いた sample.jsonl。線の型・根拠は Opus に見せない。
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
from scripts.graphrag_eval.prompts import E2_QUESTION, E2_RUBRIC, EdgeVerdict
from scripts.graphrag_eval.stats import EDGE_TYPES, EdgeJudgement, e2_type_result

PREREG = "graphrag_e2_edge_precision"
SEED = 20261102
MAX_TOKENS = 1500


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def present_order(pair_key: str, seed: int = SEED) -> bool:
    """True なら事象 1 = b (鍵の後ろ側)。組ごとに決まる (並列の順に依らない)。"""
    return random.Random(f"{seed}:{pair_key}").random() < 0.5


def blind_inputs(row: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Opus に渡す入力 (線の型・根拠・確率・事象 id を含めない)。"""
    flip = present_order(row["pair_key"])
    first, second = (row["b"], row["a"]) if flip else (row["a"], row["b"])

    def strip(ev: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in ev.items() if k != "event_id"}

    return {"事象 1": strip(first), "事象 2": strip(second)}, flip


def to_judgement(row: dict[str, Any], v: EdgeVerdict, flip: bool) -> EdgeJudgement:
    tag_a, tag_b = (v.tag_2, v.tag_1) if flip else (v.tag_1, v.tag_2)
    return EdgeJudgement(
        pair_key=row["pair_key"],
        types=frozenset(row["types"]),
        relation=v.relation,
        same_attacker=v.same_attacker,
        tag_a=tag_a,
        tag_b=tag_b,
    )


async def run(rows: list[dict[str, Any]], budget: OpusBudget, out: Path) -> list[str]:
    judge = make_judge(EdgeVerdict, E2_RUBRIC, budget, max_tokens=MAX_TOKENS)
    failed: list[str] = []
    gold: dict[str, dict[str, Any]] = {}

    async def one(row: dict[str, Any]) -> None:
        inputs, flip = blind_inputs(row)
        v = await judge(E2_QUESTION, inputs)
        if not isinstance(v, EdgeVerdict):
            failed.append(row["pair_key"])
            return
        j = to_judgement(row, v, flip)
        gold[row["pair_key"]] = {
            "pair_key": row["pair_key"],
            "types": sorted(j.types),
            "sampled_as": row["sampled_as"],
            "rel_types": sorted({e.get("rel_type", "") for e in row.get("edges", [])}),
            "event_1_is_b": flip,
            "relation": v.relation,
            "same_attacker": v.same_attacker,
            "tag_a": j.tag_a,
            "tag_b": j.tag_b,
            "confidence": v.confidence,
            "reason": v.reason,
        }

    try:
        await asyncio.gather(*[one(r) for r in rows])
    finally:
        with out.open("w", encoding="utf-8") as fh:
            for k in sorted(gold):
                fh.write(json.dumps(gold[k], ensure_ascii=False) + "\n")
    return failed


def summarize(gold_path: Path) -> dict[str, Any]:
    rows = [json.loads(x) for x in gold_path.read_text("utf-8").splitlines() if x.strip()]
    js = [
        EdgeJudgement(
            pair_key=r["pair_key"],
            types=frozenset(r["types"]),
            relation=r["relation"],
            same_attacker=r["same_attacker"],
            tag_a=r["tag_a"],
            tag_b=r["tag_b"],
        )
        for r in rows
    ]
    out: dict[str, Any] = {}
    for t in EDGE_TYPES:
        res = e2_type_result(t, js)
        d = asdict(res)
        d["relation_dist"] = _dist(r["relation"] for r in rows if t in r["types"])
        d["low_confidence"] = sum(r["confidence"] == "low" for r in rows if t in r["types"])
        known = [r for r in rows if t in r["types"] and r["same_attacker"] != "不明"]
        if t == "same_actor":
            d["precision_excl_unknown"] = (
                sum(r["same_attacker"] == "はい" for r in known) / len(known) if known else None
            )
        out[t] = d
    return out


def _dist(values: Any) -> dict[str, int]:
    d: dict[str, int] = {}
    for v in values:
        d[v] = d.get(v, 0) + 1
    return d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, required=True)
    ap.add_argument("--check-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--estimate", type=int, default=102)
    args = ap.parse_args()
    d: Path = args.dir if args.dir.is_absolute() else ROOT / args.dir
    gate = require_prereg(PREREG, Path(__file__), d)
    print(json.dumps(gate, ensure_ascii=False))
    if args.check_only:
        return 0
    sample = d / "sample.jsonl"
    rows = [json.loads(x) for x in sample.read_text("utf-8").splitlines() if x.strip()]
    budget = OpusBudget("GRAPHRAG-E2", args.estimate)
    print(f"組 {len(rows)} / 見積もり {budget.estimate} / 上限 {budget.limit}")
    if args.dry_run:
        return 0
    gold = d / "gold.jsonl"
    stopped = None
    try:
        failed = asyncio.run(run(rows, budget, gold))
    except BudgetExceededError as exc:
        stopped, failed = str(exc), []
    population = json.loads((d / "population.json").read_text("utf-8"))
    manifest = {
        **gate,
        "item": "GRAPHRAG-E2",
        "judge": "claude-opus-5-5",
        "effort": "high",
        "passes": 1,
        "sample": {"path": str(sample.relative_to(ROOT)), "sha256": sha256(sample)},
        "gold": {"path": str(gold.relative_to(ROOT)), "sha256": sha256(gold)},
        "exclusion_files_sha256": population.get("exclusion_files_sha256", {}),
        "population": population,
        "opus_calls_bridge": budget.used,
        "budget_limit": budget.limit,
        "failed": failed,
        "stopped": stopped,
        "finished_at": now_iso(),
    }
    write_json(d / "manifest.json", manifest)
    summary = summarize(gold)
    write_json(d / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    print("Opus", budget.used, "失敗", len(failed), "停止", stopped)
    return 0 if stopped is None else 2


if __name__ == "__main__":
    sys.exit(main())
