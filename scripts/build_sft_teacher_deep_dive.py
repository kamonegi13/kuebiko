#!/usr/bin/env python3
"""深掘り選定 (rubric 採点) の SFT 教師対を外部 LLM で作る (2026-09-21)。

背景: 深掘りの選定は **s17 にも n17c にも学習させていない課題**だった。教師データの
一覧 (pair_judge 1510 / triage 1013 / article_summary 802 / pir_judge 800 /
event_kind 600 / ach 404 / spotlight 396 / synthesis 126) に detect も深掘りも無い。

⭐ **教師の優位を実測してから収穫する**。審判 (urgency 低 × need_to_know 高 = 深掘り
適合) で測ると、片方だけが選んだ分で:

| 腕 | urgency | 深掘り適合 |
|---|---|---|
| **Sonnet** | **0.73** | **82%** |
| n17m30 | 1.75 | 58% |

n17m30 が独自に選ぶ記事は urgency 1.75 = **速報で扱うべきもの**で、深掘りの目的
(速報性はないが把握が要る) に反していた。Sonnet は 0.73 で目的に沿う。
**教師が優れていることを確認した上での収穫**である (narrative では Opus > Sonnet を
318 対で確認してから収穫した、08-27 と同じ手順)。

⚠ 収穫は **ML 前段を通さない**。本番は top-90 に絞るが、教師は全候補ぶん要る。

⚠ 干渉に注意: 09-13 に「1 本の LoRA で 2 長文課題は無理」と実測している。深掘り採点は
**判定課題**なので s17 (判定系 5 課題を学習済み) へ足すのが筋。生成課題の n17c へ
足すと 3 つ目の課題になり、同じ失敗を踏む。

    docker exec -e LLM_LOCAL_FALLBACK=0 kuebiko python \\
        scripts/build_sft_teacher_deep_dive.py --model claudecode:sonnet --weeks 13
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.config_loader import load_app_config  # noqa: E402
from src.digest.db_filter import (  # noqa: E402
    fetch_for_deep_dive_candidates,
    fetch_recent_brief_titles,
)
from src.digest.deep_dive_selector import (  # noqa: E402
    RUBRIC_CHUNK_SIZE,
    _render_prompt,
    _WireRubricOutput,
)
from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.tools.llm_client import LLMClient, LLMError  # noqa: E402
from src.tools.model_tiers import Step, build_llm_for_ref  # noqa: E402

OUT = Path("data/mlx/teacher/deep_dive_select.jsonl")
_NOVELTY_LOOKBACK_HOURS = 672


def past_keys(repo: RunHistoryRepository, *, until: datetime) -> set[str]:
    """``until`` 時点から遡って選定済の dedup_key (**上限つき**)。"""
    since = until - timedelta(hours=_NOVELTY_LOOKBACK_HOURS)
    with repo._connect() as conn:  # noqa: SLF001 — 収穫専用の as-of 引き
        rows = conn.execute(
            "SELECT DISTINCT dedup_key FROM f1_selections "
            "WHERE selected_at >= ? AND selected_at < ? AND dedup_key IS NOT NULL",
            (since.isoformat(), until.isoformat()),
        ).fetchall()
    return {r["dedup_key"] for r in rows if r["dedup_key"]}


async def harvest_window(
    *, end: datetime, repo: RunHistoryRepository, llm: LLMClient, pool_max: int
) -> list[dict[str, Any]]:
    keys = past_keys(repo, until=end)
    pref = fetch_for_deep_dive_candidates(
        lookback_hours=168, now=end, novelty_excluded_dedup_keys=keys or None, pool_max=pool_max
    )
    if not pref.candidates:
        return []
    briefs = fetch_recent_brief_titles(lookback_hours=168, now=end)[:50]
    try:
        from src.pir.integration import build_synthesis_pir_context, get_pir_config

        pir_context = build_synthesis_pir_context(get_pir_config().priorities)
    except Exception:  # noqa: BLE001 — PIR 不在でも収穫は成立する
        pir_context = []

    out: list[dict[str, Any]] = []
    cands = pref.candidates
    for i in range(0, len(cands), RUBRIC_CHUNK_SIZE):
        chunk = cands[i : i + RUBRIC_CHUNK_SIZE]
        prompt = _render_prompt(
            items=chunk,
            recent_briefs=briefs,
            past_selected_keys=sorted(keys),
            pir_context=pir_context,
        )
        try:
            res = await llm.generate_structured(
                prompt=prompt, schema=_WireRubricOutput, temperature=0.0, max_tokens=12_000
            )
        except LLMError as exc:
            print(f"    chunk {i // RUBRIC_CHUNK_SIZE + 1} 失敗 {type(exc).__name__}", flush=True)
            continue
        # ⭐ 教師対は **prompt と completion をそのまま**残す (本番と同じ形で学習する)
        out.append(
            {
                "key": f"{end.date()}:{i // RUBRIC_CHUNK_SIZE + 1}",
                "prompt": prompt,
                "completion": res.model_dump_json(),
                "n_items": len(chunk),
                "n_scored": len(res.scored_articles),
            }
        )
    return out


async def main_async(args: argparse.Namespace) -> int:
    # ⚠ 教師は全候補ぶん要る。本番の ML 前段 (top-90) を通さない。
    os.environ.setdefault("DEEPDIVE_ML_PREFILTER", "0")
    repo = RunHistoryRepository()
    llm = build_llm_for_ref(
        args.model, Step.DIGEST_DEEP_DIVE_SELECT, load_app_config(), timeout_seconds=1800.0
    )
    print(f"教師モデル: {llm.model}")
    done: set[str] = set()
    if OUT.exists() and not args.fresh:
        done = {
            json.loads(x)["key"] for x in OUT.read_text(encoding="utf-8").splitlines() if x.strip()
        }
        print(f"既存 {len(done)} 対を読み飛ばす")
    base = datetime.fromisoformat(args.end).replace(tzinfo=UTC)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    for i in range(args.weeks):
        end = base - timedelta(days=7 * i)
        if any(k.startswith(f"{end.date()}:") for k in done):
            continue
        try:
            rows = await harvest_window(end=end, repo=repo, llm=llm, pool_max=args.pool_max)
        except Exception as exc:  # noqa: BLE001 — 1 窓の失敗で全体を落とさない
            print(f"{end.date()}  ⚠ 失敗 {type(exc).__name__}: {exc}", flush=True)
            continue
        if not rows:
            print(f"{end.date()}  候補ゼロ — 飛ばす", flush=True)
            continue
        with OUT.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        total += len(rows)
        scored = sum(r["n_scored"] for r in rows)
        items = sum(r["n_items"] for r in rows)
        print(
            f"{end.date()}  対 {len(rows):2d} / 採点 {scored:3d}/{items:3d}  (累計 {total})",
            flush=True,
        )
    print(f"書込: {OUT}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", default="claudecode:sonnet")
    p.add_argument("--end", default="2026-09-14T00:55:00")
    p.add_argument("--weeks", type=int, default=13)
    p.add_argument("--pool-max", type=int, default=180)
    p.add_argument("--fresh", action="store_true")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
