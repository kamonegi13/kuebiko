"""Phase 5T-T2 rubric LLM dry-run.

過去 watch ch 蓄積から Stage 0+1 prefilter を通った候補を抽出し、
deep_dive_selector の rubric scoring を実 LLM で 1 回実行する。
出力品質を観察して prompt 改善の判断材料にする。

Usage:
    uv run python scripts/dry_run_deep_dive.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from src.config_loader import load_app_config
from src.digest.db_filter import (
    fetch_for_deep_dive_candidates,
    fetch_recent_brief_titles,
)
from src.digest.deep_dive_selector import (
    _parse_llm_output,
    _render_prompt,
    select_deep_dive_articles,
)
from src.storage.run_history import RunHistoryRepository
from src.tools.llm_client import OllamaClient


async def main() -> None:
    cfg = load_app_config()
    repo = RunHistoryRepository()
    print("=== Phase 5T-T2 dry-run ===")
    print("DB: data/run_history.db")
    print(f"Model: {cfg.ollama_main_model}")
    print()

    # 1. 過去 F1 選定の dedup_key を取得 (novelty 判定用)
    past_keys = repo.find_recent_f1_dedup_keys(lookback_hours=672)
    print(f"過去 4 週 F1 選定済 dedup_key: {len(past_keys)} 件")

    # 2. Stage 0+1 prefilter で候補抽出
    pref = fetch_for_deep_dive_candidates(
        lookback_hours=168,
        novelty_excluded_dedup_keys=past_keys or None,
    )
    print(f"Stage counts: {json.dumps(pref.stage_counts, ensure_ascii=False)}")
    print(f"最終候補: {len(pref.candidates)} 件")
    print()

    if not pref.candidates:
        print("⚠️ 候補ゼロ。summary 永続化が transitional 完了するまで data 不足。")
        return

    # 3. context: 直近 1 週 brief/alert タイトル
    recent_briefs = fetch_recent_brief_titles(lookback_hours=168)
    print(f"今週の brief/alert: {len(recent_briefs)} 件 (LLM context として 50 件まで)")
    print()

    # 4. prompt をレンダリングしてサイズ確認
    prompt = _render_prompt(
        items=pref.candidates,
        recent_briefs=recent_briefs[:50],
        past_selected_keys=sorted(past_keys),
    )
    print(f"Prompt サイズ: {len(prompt)} chars ({len(prompt.encode('utf-8'))} bytes)")
    print()

    # 5. LLM を実行
    llm = OllamaClient(
        base_url=cfg.ollama_base_url,
        model=cfg.ollama_main_model,
    )
    print("LLM 実行中...")
    selected = await select_deep_dive_articles(
        llm=llm,
        candidates=pref.candidates,
        recent_briefs=recent_briefs[:50],
        past_selected_keys=sorted(past_keys),
    )

    # 6. 結果
    print(f"\n=== 選定結果 ({len(selected)} 件) ===\n")
    for s in selected:
        print(f"composite={s.composite:.2f}  (pir={s.pir} roi={s.roi} t={s.timeliness} n={s.novelty})")
        print(f"  feed:   {s.candidate.feed_title}")
        print(f"  title:  {s.candidate.title}")
        print(f"  rationale: {s.rationale}")
        print()

    # 7. 全 candidate の scoring 結果 dump
    print("=== 全候補 LLM 出力 (debug 用) ===")
    response = await llm.generate(
        prompt=prompt,
        temperature=0.25,
        max_tokens=4000,
        think=False,
    )
    parsed = _parse_llm_output(response.text or "")
    print(f"\nLLM 応答: {len(response.text or '')} chars, parsed {len(parsed)} entries")
    print()
    # 全 score 分布
    score_dist = {"pir": [], "roi": [], "timeliness": [], "novelty": []}
    for entry in parsed:
        sc = entry.get("scores") or {}
        if isinstance(sc, dict):
            for k in score_dist:
                v = sc.get(k)
                if isinstance(v, (int, float)):
                    score_dist[k].append(int(v))
    print("score 分布:")
    for axis, vals in score_dist.items():
        if vals:
            dist = {i: vals.count(i) for i in range(6)}
            print(f"  {axis:<11}: avg={sum(vals)/len(vals):.2f}  dist={dist}")

    # raw 出力 save
    out_path = Path("scripts/dry_run_output.json")
    out_path.write_text(
        json.dumps(
            {
                "model": cfg.ollama_main_model,
                "candidates_count": len(pref.candidates),
                "stage_counts": pref.stage_counts,
                "raw_response": response.text,
                "parsed": parsed,
                "selected_count": len(selected),
                "selected": [
                    {
                        "article_id": s.candidate.article_id,
                        "title": s.candidate.title,
                        "feed": s.candidate.feed_title,
                        "composite": s.composite,
                        "pir": s.pir,
                        "roi": s.roi,
                        "timeliness": s.timeliness,
                        "novelty": s.novelty,
                        "rationale": s.rationale,
                    }
                    for s in selected
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nraw 出力を保存: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
