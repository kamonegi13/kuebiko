#!/usr/bin/env python3
"""深掘り (weekly-recap) の凍結窓を作る (2026-09-20)。

事象ニュース 39 窓 / spotlight 30 窓と**同じ密度**で深掘りを測るため、過去の週境界で
本番と同じ入力を再構築し、narrative プロンプトだけを凍結する。

⭐ **深掘りには教師データが存在しない** (教師は事象ニュース 682 行と spotlight 459 行の
2 種類だけ。「深掘り 459 行」は spotlight の誤分類だった)。よって保存 prompt が無く、
入力は DB から組み直すしかない。

深掘りの LLM 呼出は 2 つある:

1. rubric 採点 → 60 件のプールから 12 件を選ぶ (**判定**の課題)
2. narrative 生成 → 選ばれた 12 件から本文を書く (**生成**の課題)

腕 (n17m30 / n17c / base) で差が出るのは 2 なので、**1 を凍結して 2 のプロンプトを配る**。
選定は既定で本番と同じ LLM rubric を 1 度だけ走らせて固定する (`--select top` なら
決定論 composite の上位 12。LLM を使わないぶん速いが、本番の入力分布とはずれる)。

⚠ `find_recent_f1_dedup_keys` は下限しか見ない。過去窓にそのまま使うと**窓より後の
選定**まで novelty 除外に入り、当時と違う候補になる。ここでは上限つきで自前に引く。

    DATABASE_URL=... OLLAMA_BASE_URL=... \\
        uv run python scripts/build_deep_dive_frozen.py --weeks 20 \
            --out data/mlx/frozen_deep_dive.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
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
    DEFAULT_MAX_SELECT,
    select_deep_dive_articles,
)
from src.digest.llm_digest import _render_prompt  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.tools.llm_client import LLMClient, OllamaClient  # noqa: E402
from src.tools.model_tiers import Step, build_llm_for  # noqa: E402

_TEMPLATE = "digest/weekly_recap.j2"


def past_f1_keys(repo: RunHistoryRepository, *, until: datetime, lookback_hours: int) -> set[str]:
    """``until`` 時点から遡って F1 が選定済の dedup_key (**上限つき**)。"""
    since = until - timedelta(hours=lookback_hours)
    with repo._connect() as conn:  # noqa: SLF001 — 凍結専用の as-of 引き
        rows = conn.execute(
            """
            SELECT DISTINCT dedup_key FROM f1_selections
             WHERE selected_at >= ? AND selected_at < ? AND dedup_key IS NOT NULL
            """,
            (since.isoformat(), until.isoformat()),
        ).fetchall()
    return {r["dedup_key"] for r in rows if r["dedup_key"]}


def week_label(end: datetime) -> str:
    """本番の period_label と同じ形 (週の開始日〜終了日)。"""
    start = end - timedelta(hours=168)
    return f"今週 ({start.date()} 〜 {end.date()})"


async def build_window(
    *,
    end: datetime,
    repo: RunHistoryRepository,
    llm: LLMClient | None,
) -> dict[str, Any] | None:
    keys = past_f1_keys(repo, until=end, lookback_hours=672)
    pref = fetch_for_deep_dive_candidates(
        lookback_hours=168,
        now=end,
        novelty_excluded_dedup_keys=keys or None,
    )
    if not pref.candidates:
        return None
    briefs = fetch_recent_brief_titles(lookback_hours=168, now=end)[:50]
    if llm is None:
        chosen = pref.candidates[:DEFAULT_MAX_SELECT]
        scores: list[dict[str, Any]] = []
    else:
        scored = await select_deep_dive_articles(
            llm=llm,
            candidates=pref.candidates,
            recent_briefs=briefs,
            past_selected_keys=sorted(keys),
        )
        if not scored:
            return None
        chosen = [s.candidate for s in scored]
        scores = [
            {"article_id": s.candidate.article_id, "composite": round(s.composite, 3)}
            for s in scored
        ]
    return {
        "window_end": end.isoformat(),
        "pool": len(pref.candidates),
        "selected": len(chosen),
        "article_ids": [c.article_id for c in chosen],
        "scores": scores,
        "prompt": _render_prompt(_TEMPLATE, candidates=chosen, period_label=week_label(end)),
    }


async def main_async(args: argparse.Namespace) -> int:
    cfg = load_app_config()
    repo = RunHistoryRepository()
    llm: LLMClient | None = None
    if args.select == "llm":
        llm = (
            OllamaClient(base_url=cfg.ollama_base_url, model=args.model, timeout_seconds=1800.0)
            if args.model
            else build_llm_for(Step.DIGEST_DEEP_DIVE, cfg)
        )
        print(f"選定モデル: {llm.model}")
    base = datetime.fromisoformat(args.end).replace(tzinfo=UTC)
    out_path = Path(args.out)
    rows: list[dict[str, Any]] = []
    if out_path.exists() and not args.fresh:
        rows = json.loads(out_path.read_text(encoding="utf-8"))
    done = {r["window_end"] for r in rows}

    for i in range(args.weeks):
        end = base - timedelta(days=args.step_days * i)
        if end.isoformat() in done:
            continue
        # ⚠ 1 窓の失敗で全体を落とさない (2026-09-20 に PBP 審判で全損した教訓)
        try:
            rec = await build_window(end=end, repo=repo, llm=llm)
        except Exception as exc:  # noqa: BLE001
            print(f"{end.date()}  ⚠ 失敗 {type(exc).__name__}: {exc}", flush=True)
            continue
        if rec is None:
            print(f"{end.date()}  候補ゼロ — 飛ばす", flush=True)
            continue
        rows.append(rec)
        rows.sort(key=lambda r: str(r["window_end"]), reverse=True)
        out_path.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        print(
            f"{end.date()}  プール {rec['pool']:3d} → 選定 {rec['selected']:2d}"
            f"  prompt {len(rec['prompt']):6d} 字",
            flush=True,
        )

    if rows:
        lens = sorted(len(r["prompt"]) for r in rows)
        print(f"\n{len(rows)} 窓  prompt 字数 中央 {lens[len(lens) // 2]} / 最長 {lens[-1]}")
    print(f"書込: {out_path}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--end", default="2026-09-14T00:55:00", help="最新窓の終端 (UTC)")
    p.add_argument("--weeks", type=int, default=20)
    p.add_argument("--step-days", type=int, default=7, help="7 なら窓は重ならない")
    p.add_argument("--select", choices=("llm", "top"), default="llm")
    p.add_argument("--model", default="", help="選定モデルの上書き (既定は本番の解決)")
    p.add_argument("--out", default="data/mlx/frozen_deep_dive.json")
    p.add_argument("--fresh", action="store_true")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
