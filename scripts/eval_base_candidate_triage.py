#!/usr/bin/env python3
"""s系 (構造化族) の基盤モデル候補を triage で素の容量測定する (学習前の足切り)。

data/eval/triage_goldset.json の凍結プロンプト (150件) を候補モデルにそのまま通し、
data/eval/triage_gold_v4.jsonl の Opus 裁定ラベル (importance/boundary) と比べる。
境界 (boundary=true) を除いた一致率が主指標 — 10-03 の重要度再設計評価と同じ物差し。

    docker exec kuebiko python scripts/eval_base_candidate_triage.py --model <ollama tag>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT if (_ROOT / "src").is_dir() else Path("/app")))

from pydantic import BaseModel, ConfigDict, Field  # noqa: E402
from typing import Literal  # noqa: E402

from src.config_loader import load_app_config  # noqa: E402
from src.tools.model_tiers import Step, build_llm_for_ref  # noqa: E402

GOLDSET = Path("data/eval/triage_goldset.json")
GOLD_V4 = Path("data/eval/triage_gold_v4.jsonl")


class StrictTriageDecision(BaseModel):
    """TriageDecision のデフォルト無し版 — 検証専用。

    本番の TriageDecision は全フィールドに default があり、model_json_schema() が
    "required" を省略する。Ollama の文法強制がこれを「全フィールド省略可」と解釈し、
    一部モデルで ``{}`` (→全デフォルト値) へ収束する既知の相性問題を避けるため、
    候補モデルの素の判断力を測る評価では required を明示したこの版を使う
    (本番の TriageDecision 自体は変更しない)。
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    importance: Literal["high", "medium", "low"]
    reason: str = Field(max_length=200)
    error: bool

_ORDER = {"low": 0, "medium": 1, "high": 2}


def _load_rows() -> list[dict[str, object]]:
    prompts = {r["article_id"]: r["prompt"] for r in json.loads(GOLDSET.read_text())["rows"]}
    rows: list[dict[str, object]] = []
    with GOLD_V4.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            g = json.loads(line)
            prompt = prompts.get(g["key"])
            if prompt is None:
                continue
            rows.append({**g, "prompt": prompt})
    return rows


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="Ollama タグ (例: hf.co/neody/...:bf16)")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    rows = _load_rows()
    if args.limit:
        rows = rows[: args.limit]
    print(f"件数: {len(rows)}", flush=True)

    llm = build_llm_for_ref(args.model, Step.TRIAGE, load_app_config(), timeout_seconds=120.0)
    sem = asyncio.Semaphore(2)

    async def one(row: dict[str, object]) -> dict[str, object]:
        async with sem:
            try:
                decision = await llm.generate_structured(
                    prompt=str(row["prompt"]),
                    schema=StrictTriageDecision,
                    temperature=0.0,
                    think=False,
                )
                pred = decision.importance
            except Exception as exc:  # noqa: BLE001
                print("FAIL", row["key"], type(exc).__name__, str(exc)[:120], flush=True)
                pred = None
        return {**row, "pred": pred}

    results = await asyncio.gather(*(one(r) for r in rows))

    out_path = args.out or Path(f"data/eval/base_candidate_triage_{args.model.split('/')[-1]}.jsonl")
    with out_path.open("w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    scored = [r for r in results if r["pred"] is not None]
    failed = len(results) - len(scored)
    agree = sum(1 for r in scored if r["pred"] == r["importance"])
    no_boundary = [r for r in scored if not r["boundary"]]
    agree_nb = sum(1 for r in no_boundary if r["pred"] == r["importance"])
    demoted = sum(
        1
        for r in scored
        if _ORDER[str(r["pred"])] < _ORDER[str(r["importance"])]
    )
    promoted = sum(
        1
        for r in scored
        if _ORDER[str(r["pred"])] > _ORDER[str(r["importance"])]
    )
    confusion = Counter((r["importance"], r["pred"]) for r in scored)

    print(f"失敗: {failed}")
    print(f"全体一致率: {agree}/{len(scored)} = {agree / max(len(scored), 1):.1%}")
    print(
        f"境界除く一致率: {agree_nb}/{len(no_boundary)} = "
        f"{agree_nb / max(len(no_boundary), 1):.1%}"
    )
    print(f"見逃し方向 (demoted, high/medium→low寄り): {demoted}")
    print(f"過大方向 (promoted): {promoted}")
    print("confusion (gold, pred):")
    for (g, p), n in sorted(confusion.items()):
        print(f"  {g} -> {p}: {n}")
    print(f"出力: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
