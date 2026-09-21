#!/usr/bin/env python3
"""深掘り選定の蒸留教師を収穫する (2026-09-20)。

**(A) 蒸留** — LLM rubric の採点を ML で再現してコストを下げる。
12k トークン 5 分の呼出を数秒に置き換えるのが目的で、品質の改善は狙わない。

⚠ **これは現行を真値に置く設計**である (09-04 に一度踏んだバイアス)。軌道に乗ったら
**(B) 審判ラベル**で品質側を測り直すこと。detect ML も現行由来ラベルで一度棄却され、
審判ラベルに変えたら AUC 0.84 が出た。ここでも同じ順序を踏む。

なぜ収穫が要るか: `f1_selections` は**選ばれた分しか残らない**ため、負例の目標値が
無い。`score_deep_dive_candidates` (採点だけの seam) を過去週のプールに流し、
落選分も含めた全件のスコアを書き出す。

⚠ `find_recent_f1_dedup_keys` は下限しか見ない。過去窓では**窓より後の選定**まで
novelty 除外に混ざるので、ここでは上限つきで自前に引く。

    DATABASE_URL=... OLLAMA_BASE_URL=... \\
        uv run python scripts/harvest_deep_dive_labels.py --weeks 30 \\
            --out data/mlx/deep_dive_labels.jsonl
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
    DEFAULT_COMPOSITE_THRESHOLD,
    DEFAULT_MAX_SELECT,
    score_deep_dive_candidates,
)
from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.tools.llm_client import LLMClient  # noqa: E402
from src.tools.model_tiers import Step, build_llm_for, build_llm_for_ref  # noqa: E402

_NOVELTY_LOOKBACK_HOURS = 672


def past_f1_keys(repo: RunHistoryRepository, *, until: datetime) -> set[str]:
    """``until`` 時点から遡って選定済の dedup_key (**上限つき**)。"""
    since = until - timedelta(hours=_NOVELTY_LOOKBACK_HOURS)
    with repo._connect() as conn:  # noqa: SLF001 — 収穫専用の as-of 引き
        rows = conn.execute(
            """
            SELECT DISTINCT dedup_key FROM f1_selections
             WHERE selected_at >= ? AND selected_at < ? AND dedup_key IS NOT NULL
            """,
            (since.isoformat(), until.isoformat()),
        ).fetchall()
    return {r["dedup_key"] for r in rows if r["dedup_key"]}


async def harvest_window(
    *, end: datetime, repo: RunHistoryRepository, llm: LLMClient, pool_max: int
) -> list[dict[str, Any]]:
    keys = past_f1_keys(repo, until=end)
    # ⭐ **切り口の外側まで採点する**。関門 A (約 950 → 60) を学習するには、落ちた側の
    #   目標値が要る。プール 60 は high 記事の 33% しか覆わず、180 で 96% (実測)。
    pref = fetch_for_deep_dive_candidates(
        lookback_hours=168,
        now=end,
        novelty_excluded_dedup_keys=keys or None,
        pool_max=pool_max,
    )
    if not pref.candidates:
        return []
    briefs = fetch_recent_brief_titles(lookback_hours=168, now=end)[:50]
    scored = await score_deep_dive_candidates(
        llm=llm,
        candidates=pref.candidates,
        recent_briefs=briefs,
        past_selected_keys=sorted(keys),
    )
    by_id = {s.candidate.article_id: s for s in scored}
    # 選抜は本番と同じ規則で再現する (閾値 → 上限)。ML の目標は composite だが、
    # 「選ばれたか」も評価指標として要る (上位 N の一致率で測るため)。
    # 本番の選抜は**関門 A の内側 (上位 60)** でしか起きない。教師の "selected" も
    # その規則で再現する (180 全件から選ぶのは本番に無い挙動)。
    inside = {c.article_id for c in pref.candidates[:60]}
    picked = [
        s.candidate.article_id
        for s in scored
        if s.composite >= DEFAULT_COMPOSITE_THRESHOLD and s.candidate.article_id in inside
    ][:DEFAULT_MAX_SELECT]
    out: list[dict[str, Any]] = []
    for c in pref.candidates:
        s = by_id.get(c.article_id)
        out.append(
            {
                "window_end": end.isoformat(),
                "article_id": c.article_id,
                # ⚠ 採点されなかった候補は None にする。0 を入れると「最低評価」という
                #   別の教師になってしまう (欠測と最低点は違う)。
                "composite": round(s.composite, 4) if s else None,
                "pir": s.pir if s else None,
                "roi": s.roi if s else None,
                "timeliness": s.timeliness if s else None,
                "novelty": s.novelty if s else None,
                "selected": c.article_id in picked,
                "in_gate_a": c.article_id in inside,  # 本番なら LLM に届いていたか
                "pool_size": len(pref.candidates),
            }
        )
    return out


async def main_async(args: argparse.Namespace) -> int:
    cfg = load_app_config()
    repo = RunHistoryRepository()
    # ⚠ prefix (claudecode: / anthropic:) を解釈する factory を通す。
    #   OllamaClient を直に作ると外部 ref が 404 になる (2026-09-21 に踏んだ)。
    llm: LLMClient = (
        build_llm_for_ref(args.model, Step.DIGEST_DEEP_DIVE_SELECT, cfg, timeout_seconds=1800.0)
        if args.model
        else build_llm_for(Step.DIGEST_DEEP_DIVE_SELECT, cfg)
    )
    print(f"採点モデル: {llm.model}")
    out_path = Path(args.out)
    done: set[str] = set()
    if out_path.exists() and not args.fresh:
        done = {
            json.loads(line)["window_end"]
            for line in out_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        print(f"既存 {len(done)} 窓を読み飛ばす")

    base = datetime.fromisoformat(args.end).replace(tzinfo=UTC)
    total = 0
    for i in range(args.weeks):
        end = base - timedelta(days=args.step_days * i)
        if end.isoformat() in done:
            continue
        # ⚠ 1 窓の失敗で全体を落とさない
        try:
            rows = await harvest_window(end=end, repo=repo, llm=llm, pool_max=args.pool_max)
        except Exception as exc:  # noqa: BLE001
            print(f"{end.date()}  ⚠ 失敗 {type(exc).__name__}: {exc}", flush=True)
            continue
        if not rows:
            print(f"{end.date()}  候補ゼロ — 飛ばす", flush=True)
            continue
        with out_path.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        total += len(rows)
        pos = sum(1 for r in rows if r["selected"])
        unscored = sum(1 for r in rows if r["composite"] is None)
        print(
            f"{end.date()}  候補 {len(rows):3d}  正例 {pos:3d}  未採点 {unscored:3d}"
            f"  (累計 {total})",
            flush=True,
        )
    print(f"書込: {out_path}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--end", default="2026-09-14T00:55:00", help="最新窓の終端 (UTC)")
    p.add_argument("--weeks", type=int, default=30)
    p.add_argument("--step-days", type=int, default=7, help="7 なら窓は重ならない")
    p.add_argument("--model", default="", help="採点モデルの上書き (既定は本番の解決)")
    p.add_argument("--pool-max", type=int, default=180, help="採点するプール幅 (本番は 60)")
    p.add_argument("--out", default="data/mlx/deep_dive_labels.jsonl")
    p.add_argument("--fresh", action="store_true")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
