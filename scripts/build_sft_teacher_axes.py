#!/usr/bin/env python3
"""深刻度の軸 (SEVERITY_AXES) の SFT 教師対を外部 LLM で作る (2026-09-29)。

背景: s21 は軸を学習していない。Opus の裁定 (s21 と Sonnet の食い違い 100 記事) で、s21 は
標的 22%・実害の確認 20%・被害の性質 19%・広がり 14% の欄を誤った。向きは一貫して**被害者の
いない記事 (脆弱性の公表・研究) に被害を付ける**過大評価。「s17 より上か」でなく正しさで
判断する (利用者) ため、教師を作って s22 へ入れる。

入力は本番と同じ (``severity_axes.build_prompt`` = 見出し + 要約)。評価に使った記事
(data/mlx/severity_*ids*.txt と severity_axes.jsonl) は除く。DB には何も書かない。

    docker compose run --rm --no-deps -T -e LLM_LOCAL_FALLBACK=0 \\
        -v $PWD/src:/app/src:ro -v $PWD/scripts:/app/scripts:ro -v $PWD/data:/app/data \\
        kuebiko python scripts/build_sft_teacher_axes.py --max 450
出力: data/mlx/teacher/severity_axes.jsonl (再開可能)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.config_loader import load_app_config  # noqa: E402
from src.cti.severity_axes import SeverityAxes, build_prompt  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.tools.llm_client import LLMClient  # noqa: E402
from src.tools.model_tiers import Step, build_llm_for_ref  # noqa: E402

D = _ROOT / "data" / "mlx"
OUT = D / "teacher" / "severity_axes.jsonl"
_SEED = 929
_CONCURRENCY = 2
_MIN_SUMMARY_CHARS = 80
#: 教師の出力の上限 (欄 7 つの JSON)
_MAX_TOKENS = 600


def excluded_ids() -> set[str]:
    """評価に使った記事 (学習に入れると評価が汚れる)。"""
    out: set[str] = set()
    for p in D.glob("severity*ids*.txt"):
        out |= {x.strip() for x in p.read_text(encoding="utf-8").splitlines() if x.strip()}
    for name in ("severity_axes.jsonl",):
        path = D / name
        if path.exists():
            out |= {json.loads(x)["article_id"] for x in path.open(encoding="utf-8") if x.strip()}
    return out


def candidates(days: int) -> list[tuple[str, str, str]]:
    repo = RunHistoryRepository()
    with repo._connect() as conn:  # noqa: SLF001 — 読み取りのみ
        rows = conn.execute(
            "SELECT DISTINCT ON (article_id) article_id, title, summary FROM articles "
            "WHERE summary IS NOT NULL AND length(summary) >= ? "
            "AND created_at >= NOW() - (? || ' days')::interval "
            "ORDER BY article_id, created_at DESC",
            (_MIN_SUMMARY_CHARS, str(days)),
        ).fetchall()
    return [(str(r[0]), str(r[1] or ""), str(r[2] or "")) for r in rows]


async def _one(llm: LLMClient, sem: asyncio.Semaphore, row: tuple[str, str, str]) -> Any:
    aid, title, summary = row
    prompt = build_prompt(title, summary)
    async with sem:
        try:
            res = await llm.generate_structured(
                prompt, SeverityAxes, temperature=0.0, max_tokens=_MAX_TOKENS, think=False
            )
        except Exception as exc:  # noqa: BLE001
            print(f"{aid} 失敗 {type(exc).__name__}", flush=True)
            return None
    return {"key": aid, "prompt": prompt, "completion": res.model_dump_json()}


async def main_async(args: argparse.Namespace) -> int:
    llm = build_llm_for_ref(args.model, Step.SEVERITY_AXES, load_app_config())
    print(f"教師モデル: {llm.model}", flush=True)
    done = (
        {json.loads(x)["key"] for x in OUT.read_text(encoding="utf-8").splitlines() if x.strip()}
        if OUT.exists()
        else set()
    )
    skip = excluded_ids()
    pool = [r for r in candidates(args.days) if r[0] not in skip]
    rng = random.Random(_SEED)
    picked = [r for r in rng.sample(pool, min(args.max, len(pool))) if r[0] not in done]
    print(f"候補 {len(pool)} (評価用 {len(skip)} を除外) / 既存 {len(done)} / 今回 {len(picked)}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(_CONCURRENCY)
    n = 0
    with OUT.open("a", encoding="utf-8") as fh:
        for fut in asyncio.as_completed([_one(llm, sem, r) for r in picked]):
            row = await fut
            if row:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
                n += 1
    print(f"書込 {n} 件: {OUT}", flush=True)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", default="claudecode:opus")
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--max", type=int, default=450)
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
