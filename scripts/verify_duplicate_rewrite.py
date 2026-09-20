#!/usr/bin/env python3
"""重複の書き直しが実際に効くかを凍結入力で測る (2026-09-20)。

⚠ 上限 (`maxItems`) は最大の暴走を消したが (窓 7: facts 84/重複 64 → 18/0)、
**重複そのものは残る** (窓 13: 重複 1 → 12)。上限は要素数を強制するだけで、
同じ行を上限まで並べることは止められない (uniqueItems は文脈自由文法で表現不可)。

重複の書き直しは `runner._rewrite_hints` にあり、**評価経路 (`generate_draft` 直呼び)
では発火しない**。ここで「重複つき出力 → 指摘 → 書き直し」を通し、重複が消えるかを測る。

⭐ **関門を入れたら、効くことを測る**。入れたこと自体を効果と数えない (本日の反復教訓)。

    docker exec kuebiko python /app/scripts/verify_duplicate_rewrite.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.config_loader import load_app_config  # noqa: E402
from src.eventnews import structure_metrics  # noqa: E402
from src.eventnews.models import EventNewsDraft, FactItem  # noqa: E402
from src.tools.model_tiers import Step, build_llm_for  # noqa: E402

D = Path("/app/data/mlx") if Path("/app/data/mlx").exists() else _ROOT / "data" / "mlx"


def _items(xs: list[Any]) -> list[FactItem]:
    return [
        FactItem(text=str(x.get("text", "")), source_index=int(x.get("source_index") or 0))
        for x in xs
        if isinstance(x, dict)
    ]


def _draft_of(raw: str) -> EventNewsDraft | None:
    try:
        o = json.loads(raw)
    except Exception:  # noqa: BLE001 — 壊れた出力は測れない
        return None
    return EventNewsDraft(
        headline=o.get("headline", ""),
        bluf=o.get("bluf", ""),
        key_points=list(o.get("key_points") or []),
        facts=_items(list(o.get("facts") or [])),
        discrepancies=_items(list(o.get("discrepancies") or [])),
        caveats=_items(list(o.get("caveats") or [])),
        unknowns=[str(x) for x in (o.get("unknowns") or [])],
    )


def _dups(d: EventNewsDraft) -> int:
    return structure_metrics.duplicate_lines(d.facts) + structure_metrics.duplicate_lines(
        d.discrepancies
    )


async def main_async(args: argparse.Namespace) -> int:
    rows = json.loads((D / args.eval_file).read_text(encoding="utf-8"))
    cfg = load_app_config()
    llm = build_llm_for(Step.EVENT_NEWS, cfg)
    from src.eventnews import generator as gen

    targets: list[tuple[int, EventNewsDraft, str]] = []
    for i, r in enumerate(rows):
        d = _draft_of(r.get("ollama_generated") or "")
        if d is not None and _dups(d) > 0:
            targets.append((i, d, r.get("prompt") or ""))
    print(f"重複のある窓 {len(targets)} 件: {[i for i, _, _ in targets]}", flush=True)

    out: list[dict[str, Any]] = []
    for i, before, prompt in targets:
        hint = structure_metrics.duplicate_hint(
            {"facts": before.facts, "discrepancies": before.discrepancies}
        )
        if hint is None:
            continue
        # ⚠ 凍結入力を **本番と同じ prompt** で再生成する (members を再構成せず、
        #    保存 prompt に指摘を足す形。経路は違うが「指摘が効くか」は測れる)
        try:
            resp = await llm.generate_structured(
                prompt=prompt + "\n\n# 書き直しの指示\n" + hint,
                schema=EventNewsDraft,
                temperature=0.3,
                max_tokens=gen.EVENT_NEWS_MAX_TOKENS,
                think=False,
            )
        except Exception as exc:  # noqa: BLE001 — 個別失敗は記録して続ける
            print(f"  窓 {i}: 失敗 {type(exc).__name__}", flush=True)
            continue
        after = _dups(resp)
        out.append({"index": i, "before": _dups(before), "after": after})
        print(f"  窓 {i}: 重複 {_dups(before)} → {after}", flush=True)

    if out:
        fixed = sum(1 for r in out if r["after"] == 0)
        print(
            f"\n重複が消えた窓 {fixed}/{len(out)}  "
            f"合計 {sum(r['before'] for r in out)} → {sum(r['after'] for r in out)}"
        )
    (D / "duplicate_rewrite_check.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--eval-file", default="eval_ollama_n17c_capped.json")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
