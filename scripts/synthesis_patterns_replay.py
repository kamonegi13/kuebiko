#!/usr/bin/env python3
"""状況総括の横断パターン版 (docs/synthesis_pattern_redesign.md) の再現・計数。

保存済みの estimate (``status_synthesis.tradecraft.grounded_estimate``) と、その窓の根拠記事の
属性 (``situation_evidence`` ほか) から、パターン版を本番外で描き直す。
台帳・本番の出力は書き換えない (DB は SELECT のみ。``generation_inputs`` / ``llm_usage`` は止める)。

使い方 (本番コンテナの中では回さない。使い捨てコンテナで):
    # P0: LLM なしで直近 30 日の「パターンが立つ日の割合・1 日の数」を数える
    python scripts/synthesis_patterns_replay.py --count-only --last 30
    # P2: 指定の窓を n20rd で描き直し JSONL に全節と関門の件数を書く
    python scripts/synthesis_patterns_replay.py --ids 589 587 --out /tmp/patterns.jsonl
    python scripts/synthesis_patterns_replay.py --last 12 --model kuebiko-sft:n20rd
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

os.environ.setdefault("GENERATION_INPUT_RECORD", "0")
os.environ.setdefault("LLM_USAGE_LOCAL", "0")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.storage.repo_judgment_attrs import JudgmentAttrs  # noqa: E402
from src.storage.run_history import RunHistoryRepository, StatusSynthesisRecord  # noqa: E402
from src.synthesis.grounded.estimate import Estimate, estimate_from_dict  # noqa: E402
from src.synthesis.grounded.pattern_render import generate_gated  # noqa: E402
from src.synthesis.grounded.pattern_sections import assemble_pattern_sections  # noqa: E402
from src.synthesis.grounded.patterns import PatternPlan, build_patterns  # noqa: E402

DEFAULT_MODEL = "kuebiko-sft:n20rd"
DEFAULT_LAST = 30
#: 窓の一覧を引く上限 (--ids は新しい順にこの範囲から探す)
LIST_LIMIT = 400
#: judge_input に載せる、判定あたりの根拠の抜粋の上限
EXCERPTS_PER_JUDGMENT = 3
JST = ZoneInfo("Asia/Tokyo")


def estimate_of(rec: StatusSynthesisRecord) -> Estimate | None:
    """保存記録から grounded_estimate を取り出す (無い・壊れは None)。"""
    try:
        data = json.loads(rec.tradecraft) if rec.tradecraft else {}
        raw = data.get("grounded_estimate") if isinstance(data, dict) else None
        return estimate_from_dict(raw) if isinstance(raw, dict) else None
    except (KeyError, TypeError, ValueError):
        return None


def period_label(est: Estimate) -> str:
    start, end = est.period_start.astimezone(JST), est.period_end.astimezone(JST)
    return f"{start:%Y-%m-%d %H:%M} 〜 {end:%Y-%m-%d %H:%M} JST"


def load_attrs(repo: RunHistoryRepository, est: Estimate) -> dict[str, JudgmentAttrs]:
    return repo.get_judgment_attrs(
        [j.id for j in est.judgments], since=est.period_start, until=est.period_end
    )


def judge_input(est: Estimate) -> str:
    """審判の公平のための入力 = 全判定の claim と、保存された根拠の抜粋 (判定あたり最大 3 件)。

    A (本番の出力) は根拠の抜粋も見て書かれているので、審判が判定の文だけを事実の源にすると
    A の誤りが過大になる。
    """
    lines: list[str] = []
    for j in est.judgments:
        lines.append(f"[{j.id}] {j.claim}")
        lines.extend(f"  - 根拠の抜粋: {e.excerpt}" for e in j.evidence[:EXCERPTS_PER_JUDGMENT])
    return "\n".join(lines)


def count_row(rec: StatusSynthesisRecord, est: Estimate, plan: PatternPlan) -> dict[str, Any]:
    return {
        "id": rec.id,
        "day": est.period_start.astimezone(JST).date().isoformat(),
        "judgments": len(est.judgments),
        "moved": plan.moved_count,
        "patterns": len(plan.patterns),
        "pattern_sizes": [p.count for p in plan.patterns],
        "rest": len(plan.rest),
        "notable": len(plan.notable),
        "has_pattern": bool(plan.patterns),
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    with_pattern = sum(r["has_pattern"] for r in rows)
    return {
        "days": n,
        "days_with_pattern": with_pattern,
        "days_without_pattern": n - with_pattern,
        "patterns_per_day_mean": round(sum(r["patterns"] for r in rows) / n, 2) if n else 0.0,
        "patterns_per_day_max": max((r["patterns"] for r in rows), default=0),
    }


def select_records(
    repo: RunHistoryRepository, *, ids: list[int] | None, last: int
) -> list[StatusSynthesisRecord]:
    records = repo.list_synthesis(period_type="daily", limit=LIST_LIMIT if ids else last)
    if ids:
        wanted = set(ids)
        return [r for r in records if r.id in wanted]
    return records


async def render_row(
    rec: StatusSynthesisRecord, est: Estimate, plan: PatternPlan, llm: Any
) -> dict[str, Any]:
    label = period_label(est)
    gated, stats = await generate_gated(
        llm=llm, est=est, plan=plan, period_label=label, moved_questions=()
    )
    sections = assemble_pattern_sections(est=est, plan=plan, gated=gated)
    return {
        **count_row(rec, est, plan),
        "model": getattr(llm, "model", ""),
        "gate_stats": stats.__dict__ if stats else None,
        "sections": sections.model_dump(),
        "judge_input": judge_input(est),
        "stored": {"headline": rec.headline, "weight_section": rec.weight_section},
    }


def _build_llm(model: str) -> Any:
    from src.config_loader import load_app_config
    from src.tools.model_tiers import Step, build_llm_for_ref

    return build_llm_for_ref(model, Step.SYNTHESIS_NARRATIVE, load_app_config())


async def run(args: argparse.Namespace) -> int:
    repo = RunHistoryRepository()
    llm = None if args.count_only else _build_llm(args.model)
    rows: list[dict[str, Any]] = []
    for rec in select_records(repo, ids=args.ids, last=args.last):
        est = estimate_of(rec)
        if est is None:
            print(f"skip id={rec.id}: no grounded_estimate", file=sys.stderr)
            continue
        plan = build_patterns(est.judgments, load_attrs(repo, est))
        row = count_row(rec, est, plan) if llm is None else await render_row(rec, est, plan, llm)
        rows.append(row)
        print(json.dumps({k: row[k] for k in count_row(rec, est, plan)}, ensure_ascii=False))
    print(json.dumps({"summary": summarize(rows)}, ensure_ascii=False))
    if args.out and not args.count_only:
        Path(args.out).write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
        )
    return 0 if rows else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--ids", type=int, nargs="*", help="status_synthesis.id (daily)")
    ap.add_argument("--last", type=int, default=DEFAULT_LAST, help="直近 N 本 (--ids 無しのとき)")
    ap.add_argument("--count-only", action="store_true", help="LLM を呼ばず計数だけ")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="narrative の model ref")
    ap.add_argument("--out", default="", help="JSONL の出力先 (--count-only では使わない)")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
