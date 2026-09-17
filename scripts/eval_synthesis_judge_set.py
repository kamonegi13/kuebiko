#!/usr/bin/env python3
"""凍結審判 15 窓 (data/mlx/synthesis_judge_set*.json の key) を候補モデルで生成する (2026-09-17)。

プロンプトは**現在の render seam** (``build_render_plan``、SIR 参照化後) で窓の estimate から
組み直す (judge set に保存された prompt は旧形式なので使わない)。生成物は
data/mlx/eval_synthesis_<tag>.json に {key, prompt, output(JSON 文字列)} で保存し、
対読審判 (Sonnet) の入力にする。

⚠ DB を読むのでコンテナ内で。Ollama は host.docker.internal 経由:
    docker compose run --rm --no-deps -T kuebiko python scripts/eval_synthesis_judge_set.py \\
        --model kuebiko-sft:n17c --cot --out data/mlx/eval_synthesis_n17c.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.build_sft_teacher_synthesis import (  # noqa: E402
    JUDGE_SET_GLOB,
    reserved_keys,
    select_windows,
)
from src.config_loader import load_app_config  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.synthesis.grounded.render import (  # noqa: E402
    _MAX_TOKENS,
    _TEMPERATURE,
    _WireSections,
    _WireSectionsCoT,
)
from src.tools.model_tiers import Step, build_llm_for_ref  # noqa: E402

_SCAN = 600


async def main_async(args: argparse.Namespace) -> int:
    keys = reserved_keys(sorted(Path().glob(JUDGE_SET_GLOB)))
    repo = RunHistoryRepository()
    windows, _ = select_windows(
        repo.list_synthesis(period_type=args.period, limit=_SCAN),
        period_type=args.period,
        reserved=set(),
        reserve_before=datetime.now(UTC) + timedelta(days=1),
        cot=args.cot,
    )
    targets = [w for w in windows if w.key in keys]
    print(f"審判窓 {len(keys)} / 再構築できた窓 {len(targets)} (cot={args.cot})")
    if args.dry_run:
        for w in targets:
            print(f"  {w.key} prompt {len(w.prompt)} 字 / 判定 {w.judgments}")
        return 0
    done: dict[str, dict[str, str]] = {}
    if args.out.exists():
        done = {r["key"]: r for r in json.loads(args.out.read_text(encoding="utf-8"))}
    llm = build_llm_for_ref(args.model, Step.SYNTHESIS_NARRATIVE, load_app_config())
    schema: type[_WireSections] | type[_WireSectionsCoT] = (
        _WireSectionsCoT if args.cot else _WireSections
    )
    for w in targets:
        if w.key in done:
            continue
        try:
            r = await llm.generate_structured(
                w.prompt, schema, temperature=_TEMPERATURE, max_tokens=_MAX_TOKENS, think=False
            )
            out = json.dumps(r.model_dump(), ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 — 1 窓の失敗で全体を落とさない
            print(f"  {w.key} FAIL {type(exc).__name__}: {str(exc)[:80]}", flush=True)
            continue
        done[w.key] = {"key": w.key, "prompt": w.prompt, "output": out, "model": args.model}
        args.out.write_text(
            json.dumps(list(done.values()), ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"  {w.key} ok ({len(out)} 字)", flush=True)
    print(f"完了: {len(done)}/{len(targets)} → {args.out}")
    return 0 if done else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True, help="候補モデル ref (例 kuebiko-sft:n17c)")
    ap.add_argument("--period", default="daily")
    ap.add_argument("--cot", action="store_true", help="analysis_notes つき schema で生成")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--dry-run", action="store_true")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
