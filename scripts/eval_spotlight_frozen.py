#!/usr/bin/env python3
"""spotlight を凍結窓で生成する (2026-09-20)。

事象ニュース (39 窓 / 決定論 6 指標 / 採点者 3 者) と**同じ密度**で spotlight を測るため。
§53 は 6 窓・key_events の件数だけで「退行」と断じ、その根拠 (126 件) は 8 回引き直して
再現しなかった。⭐ **件数が正常なことと、内容が良いことは別**。

出力は `eval_ollama_*.json` と**同じ形** (prompt / ollama_generated) にする —
PBP 審判 (`judge_eventnews_pbp.py`) と決定論の指標をそのまま流用できる。

⚠ 本番の文面 (v3.1 観点リスト) を差し込む。教師収穫時の保存 prompt のままだと
**本番と違う経路を測る** (2026-09-19 にこれで暴走が再現せず誤読しかけた)。

    docker exec kuebiko python /app/scripts/eval_spotlight_frozen.py \
        --model kuebiko-sft:n17m30 --n 30
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.config_loader import load_app_config  # noqa: E402
from src.spotlight.generator import _LLMSpotlightOutput  # noqa: E402
from src.tools.llm_client import OllamaClient  # noqa: E402

D = Path("/app/data/mlx") if Path("/app/data/mlx").exists() else _ROOT / "data" / "mlx"
TEACHER = D / "teacher" / "spotlight3_ok.jsonl"
GAP_SCRIPT = D / "spotlight_gap_n17c.py"


def graft_v31(prompt: str) -> tuple[str, bool]:
    """保存 prompt を本番 rubric の文面へ。置換文面は既存 script の**定義から**読む。"""
    tree = ast.parse(GAP_SCRIPT.read_text(encoding="utf-8"))
    c: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            t = node.targets[0]
            if isinstance(t, ast.Name) and isinstance(node.value.value, str):
                c[t.id] = node.value.value
    out = prompt.replace(c["OLD_CAVEATS"], c["NEW_CAVEATS"]).replace(
        c["OLD_UNKNOWNS"], c["NEW_UNKNOWNS"]
    )
    return out, out != prompt


def pick(rows: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """PIR を巡回して選ぶ (特定 PIR に偏らせない)。"""
    by_pir: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_pir.setdefault(str(r["key"]).split(":")[0], []).append(r)
    for v in by_pir.values():
        v.sort(key=lambda r: str(r["key"]), reverse=True)  # 新しい窓から
    out: list[dict[str, Any]] = []
    depth = 0
    while len(out) < n and depth < 20:
        for pir in sorted(by_pir):
            if depth < len(by_pir[pir]) and len(out) < n:
                out.append(by_pir[pir][depth])
        depth += 1
    return out


async def main_async(args: argparse.Namespace) -> int:
    rows = [json.loads(x) for x in TEACHER.read_text(encoding="utf-8").splitlines() if x.strip()]
    targets = pick(rows, args.n)
    print(
        f"凍結窓 {len(targets)} 件 / {len({str(r['key']).split(':')[0] for r in targets})} PIR",
        flush=True,
    )

    cfg = load_app_config()
    llm = OllamaClient(base_url=cfg.ollama_base_url, model=args.model, timeout_seconds=900.0)
    out_path = D / args.out
    out: list[dict[str, Any]] = []
    if out_path.exists() and not args.fresh:
        out = json.loads(out_path.read_text(encoding="utf-8"))
    done = {r["key"] for r in out}

    for r in targets:
        key = str(r["key"])
        if key in done:
            continue
        prompt, grafted = graft_v31(str(r["prompt"]))
        if not grafted:
            print(f"  ⚠ {key}: v3.1 置換対象が無い — 本番文面で測れていない", flush=True)
            continue
        try:
            resp = await llm.generate_structured(
                prompt, schema=_LLMSpotlightOutput, temperature=0.3, max_tokens=6144, think=False
            )
            gen = resp.model_dump_json()
            n_ev = len(resp.key_events)
        except Exception as exc:  # noqa: BLE001 — 個別失敗は記録して続ける
            print(f"  {key}: 失敗 {type(exc).__name__}", flush=True)
            continue
        out.append({"key": key, "prompt": prompt, "ollama_generated": gen})
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  {key:34s} key_events {n_ev:3d}{' ⚠上限' if n_ev >= 15 else ''}", flush=True)
    print(f"\n書込: {out_path} ({len(out)} 件)")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", required=True)
    p.add_argument("--n", type=int, default=30)
    p.add_argument("--out", default="")
    p.add_argument("--fresh", action="store_true")
    a = p.parse_args()
    if not a.out:
        a.out = f"eval_spotlight_{a.model.split(':')[-1]}.json"
    return asyncio.run(main_async(a))


if __name__ == "__main__":
    raise SystemExit(main())
