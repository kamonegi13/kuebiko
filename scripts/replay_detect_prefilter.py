#!/usr/bin/env python3
"""detect の replay — 同じ日を 2 つの腕で走らせて開設される claim を比べる。

``--mode input``  : 入力の広さ (全件 / ML 候補セット) を比べる。ML 前段の移行条件。
``--mode prompt`` : 判定基準 (現行 / 追跡価値の関門つき detect_new_v2.j2) を比べる。候補セット固定。

移行条件の最後の 1 つ (SYNTHESIS §47 追記 9): 候補を絞った状態で LLM が書く claim の質が、
全件投入時と同等以上か。これは凍結データの採点では測れない (LLM を両方の入力で走らせる必要がある)
ため、過去日のプールを再構築して `detect_new_claims` を 2 回呼び、開設された claim を比べる。

⚠ **台帳には一切書かない** (detect_new_claims は LLM を呼ぶだけ。2026-09-06 の収穫汚染の教訓)。
採点は既存の審判ラベル (data/mlx/detect_goldset.jsonl): 各腕が開設に使った記事のうち
審判が gold_open / watch / drop とした件数。

ホストで (GPU が空いていること。学習中は走らせない):
    DATABASE_URL="postgresql://kuebiko:...@127.0.0.1:5433/kuebiko" \\
    OLLAMA_BASE_URL=http://127.0.0.1:11434 \\
        uv run python scripts/replay_detect_prefilter.py --days 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.assessment.situation_store import SituationStore  # noqa: E402
from src.config_loader import load_app_config  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.synthesis.grounded.detect_ml import (  # noqa: E402
    build_detect_articles,
    compose_llm_candidates,
    floor_article_ids,
    is_rollup_title,
    load_detect_model,
    prefilter_top_k,
    score_articles,
)
from src.synthesis.grounded.incremental import DetectResult, detect_new_claims  # noqa: E402
from src.tools.model_tiers import Step, build_llm_for  # noqa: E402

D = Path("data/mlx")
GOLD = D / "detect_goldset.jsonl"
KIND_FILES = (D / "detect_gold_kinds.jsonl", D / "detect_heldout_kinds.jsonl")
_LOOKBACK_HOURS = 24
_POOL_LIMIT = 500


def _gold() -> dict[str, dict[str, Any]]:
    if not GOLD.exists():
        return {}
    return {
        str(json.loads(x)["article_id"]): json.loads(x)
        for x in GOLD.read_text(encoding="utf-8").splitlines()
        if x.strip()
    }


def _kinds() -> dict[str, str]:
    out: dict[str, str] = {}
    for f in KIND_FILES:
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    r = json.loads(line)
                    out[str(r["article_id"])] = str(r["kind"])
    return out


def pool_for_day(
    repo: RunHistoryRepository, day: str, *, limit: int = _POOL_LIMIT
) -> list[dict[str, Any]]:
    """その日の 24h 窓で posted かつ importance high/medium の記事 (本番プールの再構築)。"""
    end = datetime.fromisoformat(f"{day}T23:59:59+00:00")
    start = end - timedelta(hours=_LOOKBACK_HOURS)
    with repo._connect() as conn:  # noqa: SLF001 — 評価スクリプトの接続 seam 共有
        rows = conn.execute(
            "SELECT article_id, title, summary, feed_title, importance, category"
            " FROM articles WHERE status='posted' AND importance IN ('high','medium')"
            " AND created_at >= ? AND created_at <= ?"
            " ORDER BY CASE importance WHEN 'high' THEN 0 ELSE 1 END, created_at DESC LIMIT ?",
            (start.isoformat(), end.isoformat(), limit),
        ).fetchall()
    return [
        {
            "article_id": str(r["article_id"]),
            "title": str(r["title"] or ""),
            "summary": str(r["summary"] or "")[:600],
            "feed_title": str(r["feed_title"] or ""),
            "importance": str(r["importance"] or ""),
            "category": str(r["category"] or ""),
        }
        for r in rows
    ]


def narrow(
    pool: list[dict[str, Any]], scores: dict[str, float], arts: dict[str, Any], *, top_k: int
) -> list[dict[str, Any]]:
    """本番と同じ合成規則で候補セットを作る (勧告を除外、下限保証を足す)。"""
    ids = [str(a["article_id"]) for a in pool]
    roll = {a for a in ids if a in arts and is_rollup_title(arts[a].title)}
    floor = floor_article_ids({a: arts[a] for a in ids if a in arts})
    keep = set(compose_llm_candidates(scores, top_k=top_k, high_ids=floor, excluded=roll))
    return [a for a in pool if str(a["article_id"]) in keep]


def score_arm(result: DetectResult, gold: dict[str, dict[str, Any]]) -> dict[str, int]:
    """開設された claim を審判ラベルで採点する (記事単位。未審判は別に数える)。"""
    aids = {a for c in result.open for a in c.article_ids}
    out = {
        "claims": len(result.open),
        "articles": len(aids),
        "open": 0,
        "watch": 0,
        "drop": 0,
        "unjudged": 0,
    }
    for a in aids:
        g = gold.get(a)
        if g is None:
            out["unjudged"] += 1
        elif g["gold_open"]:
            out["open"] += 1
        elif g["watch"]:
            out["watch"] += 1
        else:
            out["drop"] += 1
    return out


async def main_async(args: argparse.Namespace) -> int:
    gold = _gold()
    kinds = _kinds()
    repo = RunHistoryRepository()
    model = load_detect_model()
    if model is None:
        print("モデルが無い (config/models/detect_model.json)", file=sys.stderr)
        return 1
    days = args.day or []
    if not days:
        # 審判ラベルのある期間から、プールが大きい日を上位 N 日
        with repo._connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT substr(created_at,1,10) AS d, count(*) AS n FROM articles"
                " WHERE status='posted' AND importance IN ('high','medium')"
                " AND created_at >= ? AND created_at < ? GROUP BY 1 ORDER BY n DESC LIMIT ?",
                (args.since, args.until, args.days),
            ).fetchall()
        days = [str(r["d"]) for r in rows]
    print(f"対象日 {days} (top_k={prefilter_top_k()})")
    store = SituationStore(db_path=Path("data/run_history.db"))
    active_titles = [r.title for r in store.load_situations(("active",))]
    cfg = load_app_config()
    llm = build_llm_for(Step.SYNTHESIS_DETECT, cfg)
    try:
        from src.pir.integration import build_synthesis_pir_context, get_pir_config

        pir_context = build_synthesis_pir_context(get_pir_config().priorities)
    except Exception:  # noqa: BLE001 — PIR 不在でも replay は成立する
        pir_context = []
    out: list[dict[str, Any]] = []
    totals: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for day in days:
        pool = pool_for_day(repo, day)
        if len(pool) < args.min_pool:
            print(f"  {day}: プール {len(pool)} 件 — skip")
            continue
        arts = build_detect_articles(repo, [str(a["article_id"]) for a in pool], kinds)
        scores = score_articles(model, arts)
        cand = narrow(pool, scores, arts, top_k=prefilter_top_k())
        print(f"  {day}: プール {len(pool)} → 候補 {len(cand)}", flush=True)
        # 腕: input = 入力の広さ (全件 / 候補セット) / prompt = 判定基準 (現行 / 追跡価値つき)
        if args.mode == "prompt":
            arms: dict[str, tuple[list[dict[str, Any]], str]] = {
                "cur": (cand, "synthesis/detect_new.j2"),
                "v2": (cand, "synthesis/detect_new_v2.j2"),
            }
        else:
            arms = {
                "full": (pool, "synthesis/detect_new.j2"),
                "narrow": (cand, "synthesis/detect_new.j2"),
            }
        rec: dict[str, Any] = {"day": day, "pool": len(pool), "candidates": len(cand)}
        for name, (articles, template) in arms.items():
            res = await detect_new_claims(
                llm=llm,
                articles=articles,
                active_titles=active_titles,
                pir_context=pir_context,
                period_label=f"{day} (replay)",
                template=template,
            )
            s = score_arm(res, gold)
            rec[name] = {
                **s,
                "opened": [
                    {"claim": c.claim, "domain": c.domain, "article_ids": list(c.article_ids)}
                    for c in res.open
                ],
                "rejected": [{"article_id": a, "reason": r} for a, r in res.rejected][:12],
            }
            for k, v in s.items():
                totals[name][k] += v
            print(
                f"    {name:6s} claim {s['claims']} / 記事 {s['articles']} (審判 "
                f"開設 {s['open']} 見張り {s['watch']} 見送り {s['drop']} 未審判 {s['unjudged']})",
                flush=True,
            )
        out.append(rec)
        args.out.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n=== 合計 ===")
    for name in ("cur", "v2") if args.mode == "prompt" else ("full", "narrow"):
        t = totals[name]
        if not t:
            continue
        print(
            f"  {name:6s} claim {t['claims']} / 記事 {t['articles']} | "
            f"審判 開設 {t['open']} 見張り {t['watch']} 見送り {t['drop']} 未審判 {t['unjudged']}"
        )
    print(f"書込: {args.out}  (片方だけが開いた claim は JSON を目視)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=5, help="対象日数 (プールが大きい順)")
    ap.add_argument("--day", action="append", help="日付を明示 (複数可、YYYY-MM-DD)")
    ap.add_argument("--since", default="2026-08-16", help="日選定の下限 (審判ラベルのある期間)")
    ap.add_argument("--until", default="2026-09-10", help="日選定の上限")
    ap.add_argument("--min-pool", type=int, default=40, help="この件数未満の日は skip")
    ap.add_argument(
        "--mode",
        choices=("input", "prompt"),
        default="input",
        help="input = 全件 vs 候補セット / prompt = 現行 vs 追跡価値つき",
    )
    ap.add_argument("--out", type=Path, default=Path("data/mlx/replay_detect_prefilter.json"))
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
