#!/usr/bin/env python3
"""深掘り (weekly-recap) を凍結窓で生成する (2026-09-20)。

`build_deep_dive_frozen.py` が固めた narrative プロンプトを腕ごとに走らせる。
出力は `eval_ollama_*.json` と**同じ形** (prompt / ollama_generated) — PBP 審判
(`judge_eventnews_pbp.py`) と決定論の指標をそのまま流用するため。

⚠ 温度・出力上限は**本番と同じ値を production の定数から取る**。ここで数字を写すと、
本番が変わったときに黙って別物を測ることになる (2026-09-19 に spotlight でやりかけた)。

    docker exec kuebiko python /app/scripts/eval_deep_dive_frozen.py \\
        --model kuebiko-sft:n17m30 --out /app/data/mlx/eval_dd_n17m30.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.config_loader import load_app_config  # noqa: E402
from src.digest.llm_digest import DIGEST_TEMPERATURE, _digest_max_tokens  # noqa: E402
from src.tools.llm_client import LLMError, OllamaClient  # noqa: E402


async def main_async(args: argparse.Namespace) -> int:
    cfg = load_app_config()
    frozen: list[dict[str, Any]] = json.loads(Path(args.frozen).read_text(encoding="utf-8"))
    frozen = [r for r in frozen if r["selected"] >= args.min_selected][: args.n]
    client = OllamaClient(
        base_url=args.base_url or cfg.ollama_base_url,
        model=args.model,
        timeout_seconds=args.timeout,
    )
    out_path = Path(args.out)
    rows: list[dict[str, Any]] = []
    if out_path.exists() and not args.fresh:
        rows = json.loads(out_path.read_text(encoding="utf-8"))
    done = {r.get("window_end") for r in rows}

    for rec in frozen:
        if rec["window_end"] in done:
            continue
        # 本番と同じ予算 (選定件数でスケールする)
        max_tokens = _digest_max_tokens(rec["selected"])
        t0 = time.monotonic()
        text, err = "", None
        try:
            # think=False: digest はテキスト直行。Gemma 4 の thinking で本文が空になる
            res = await client.generate(
                prompt=rec["prompt"],
                temperature=DIGEST_TEMPERATURE,
                max_tokens=max_tokens,
                think=False,
            )
            text = (res.text or "").strip()
        except LLMError as exc:
            err = f"{type(exc).__name__}: {exc}"
        rows.append(
            {
                "window_end": rec["window_end"],
                "prompt": rec["prompt"],
                "ollama_generated": text,
                "ollama_error": err,
                "max_tokens": max_tokens,
                "seconds": round(time.monotonic() - t0, 1),
            }
        )
        out_path.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        print(
            f"{rec['window_end'][:10]}  {len(text):6d} 字  {rows[-1]['seconds']:6.1f}s"
            f"{'  ⚠ ' + err if err else ''}",
            flush=True,
        )

    ok = [r for r in rows if r["ollama_generated"]]
    if ok:
        lens = sorted(len(r["ollama_generated"]) for r in ok)
        secs = sorted(float(r["seconds"]) for r in ok)
        print(
            f"\n{len(ok)}/{len(rows)} 成功  字数 中央 {lens[len(lens) // 2]} "
            f"(最短 {lens[0]} / 最長 {lens[-1]})  秒 中央 {secs[len(secs) // 2]:.0f}"
        )
    print(f"書込: {out_path}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", required=True)
    p.add_argument("--frozen", default="data/mlx/frozen_deep_dive.json")
    p.add_argument("--out", required=True)
    p.add_argument("--n", type=int, default=18)
    p.add_argument("--min-selected", type=int, default=12, help="薄い窓を除く")
    p.add_argument("--base-url", default="")
    p.add_argument("--timeout", type=float, default=1800.0)
    p.add_argument("--fresh", action="store_true")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
