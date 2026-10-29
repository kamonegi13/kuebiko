#!/usr/bin/env python3
"""枠 A ① の day-0: triage goldset の**写し**に now_s (S モデルの判定) を書く (GPU-TOOLS-DAY0)。

- 入力の goldset は読むだけ。出力は固定の別パス (既定 data/eval/triage_drift_day0_s24ci65.json) で、
  入力と同じパスなら拒否する。now_26b は残し、now_s の欄を足す。
- 客は本番と同じ経路 (``build_llm_for_ref(model, Step.TRIAGE)``: 課題の接頭辞も本番と同じ)。
- 入力・出力の sha256 を出力の meta と ``<out>.sha256.json`` に残す。

    python scripts/gpu_window/day0_triage.py --dry-run          # 計画だけ (生成なし)
    python scripts/gpu_window/day0_triage.py                    # 枠の中で (使い捨てのコンテナ)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from gpu_window_common import sha256_file  # noqa: E402

GOLDSET = Path("data/eval/triage_goldset.json")
OUT = Path("data/eval/triage_drift_day0_s24ci65.json")
MODEL = "kuebiko-sft:s24ci65"


def check_paths(src: Path, out: Path) -> None:
    if src.resolve() == out.resolve():
        raise ValueError(f"出力が入力と同じパス: {src} (原本は上書きしない)")


def target_rows(rows: Sequence[dict[str, Any]], limit: int = 0) -> list[dict[str, Any]]:
    picked = [r for r in rows if r.get("now_26b")]
    return picked[:limit] if limit else picked


def build_output(
    goldset: dict[str, Any],
    verdicts: dict[str, str | None],
    *,
    model: str,
    input_sha256: str,
) -> dict[str, Any]:
    """goldset の写し。now_26b は残し now_s を足す (判定できなかった行は None)。"""
    rows = [
        {**r, "now_s": verdicts[r["article_id"]]} if r["article_id"] in verdicts else dict(r)
        for r in goldset["rows"]
    ]
    meta = {
        **goldset.get("meta", {}),
        "now_s_model": model,
        "now_s_input_sha256": input_sha256,
        "now_s_judged": sum(1 for v in verdicts.values() if v is not None),
        "now_s_failed": sum(1 for v in verdicts.values() if v is None),
    }
    return {**goldset, "meta": meta, "rows": rows}


def write_with_hashes(out: Path, payload: dict[str, Any], input_sha256: str) -> dict[str, str]:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    hashes = {"input_sha256": input_sha256, "output_sha256": sha256_file(out)}
    out.with_name(out.name + ".sha256.json").write_text(json.dumps(hashes, indent=2), "utf-8")
    return hashes


async def judge_all(rows: Sequence[dict[str, Any]], model: str) -> dict[str, str | None]:
    from src.config_loader import load_app_config
    from src.tools.article_triage import TriageDecision
    from src.tools.model_tiers import Step, build_llm_for_ref

    llm = build_llm_for_ref(model, Step.TRIAGE, load_app_config())
    out: dict[str, str | None] = {}
    for i, r in enumerate(rows, 1):
        try:
            v = await llm.generate_structured(
                prompt=r["prompt"], schema=TriageDecision, temperature=0.0, think=False
            )
            out[r["article_id"]] = str(v.importance)
        except Exception as e:  # noqa: BLE001 - 1 件の失敗で枠を落とさない (None で残す)
            print(f"  {i}/{len(rows)} FAIL {type(e).__name__}", file=sys.stderr)
            out[r["article_id"]] = None
        if i % 25 == 0:
            print(f"  {i}/{len(rows)}", flush=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--goldset", default=str(GOLDSET))
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    src, out = Path(args.goldset), Path(args.out)
    check_paths(src, out)
    goldset = json.loads(src.read_text(encoding="utf-8"))
    rows = target_rows(goldset["rows"], args.limit)
    in_sha = sha256_file(src)
    if args.dry_run:
        print(f"[day0] dry-run: 入力 {src} sha256={in_sha[:12]} 行 {len(rows)} -> 出力 {out}")
        return 0
    verdicts = asyncio.run(judge_all(rows, args.model))
    payload = build_output(goldset, verdicts, model=args.model, input_sha256=in_sha)
    print(json.dumps(write_with_hashes(out, payload, in_sha)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
