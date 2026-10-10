#!/usr/bin/env python3
"""i58 (および比較用の旧腕) を data/eval/pir_judge_gold_v2.jsonl (Opus 4.8 裁定、
2026-10-03) の物差しで測り直す (2026-10-10、精度再測定タスク)。

docs/accuracy_targets.md の「SIR の判定 | 正解率 (境界を除く) | 92% (24/26, s21)」と
docs/research/llm_training/next_models_s22_n20.md §9.1 の「s21 24/26 / s22 25/26」は、
この gold ファイル (63 件、境界 37・非境界 26) を本番の PIR 主題判定 prompt
(src.pir.llm_judge.build_judge_prompt) にそのまま通して測った値。

gold の元記事は本番 PirConfig で判定済みの (pir, article) 組だが、再取得は
``_load_posted_rows`` の 30 日ルックバックに頼らず、article_id を直接 SELECT する
(日が経つと元の 7〜30 日窓から外れて拾えなくなるため)。READ-ONLY。

使い方 (コンテナ内、本番停止不要・concurrency 1):
    docker compose run --rm --no-deps -T -e PYTHONPATH=/app \
        -v $PWD/src:/app/src:ro -v $PWD/scripts:/app/scripts:ro -v $PWD/data:/app/data \
        kuebiko python scripts/eval_i58_pir_judge_gold.py --model kuebiko-sft:i58
#
# build_llm_for_ref() が step の課題接頭辞を自動で付ける (2026-09-22 以降、本番と同形)。
# probe_triage_model.py の --task-prefix のような明示フラグは不要。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT if (_ROOT / "src").is_dir() else Path("/app")))

from src.config_loader import load_app_config  # noqa: E402
from src.pir.integration import get_pir_config  # noqa: E402
from src.pir.llm_judge import JudgeVerdict, build_judge_prompt  # noqa: E402
from src.storage.db_backend import connect  # noqa: E402
from src.tools.model_tiers import Step, build_llm_for_ref  # noqa: E402

GOLD = Path("data/eval/pir_judge_gold_v2.jsonl")
ARTICLE_COLS = (
    "article_id, title, url, feed_title, feed_url, importance, posted_channel, "
    "created_at, summary, body, victim_sector_canonical, victim_country_iso, "
    "subject_actor_ids, subject_actor_source, category, socio_political_intent, is_ransomware"
)


def _load_gold() -> list[dict[str, object]]:
    rows = []
    for line in GOLD.open(encoding="utf-8"):
        if not line.strip():
            continue
        g = json.loads(line)
        pir_id, article_id = g["key"].split(":", 1)
        rows.append({**g, "pir_id": pir_id, "article_id": article_id})
    return rows


def _fetch_articles(article_ids: list[str]) -> dict[str, object]:
    """article_id を直接指定して記事行を取る (ルックバック窓に頼らない、READ-ONLY)。

    ``connect()`` が返す cursor は ``?`` を PG の ``%s`` へ自動変換する
    (``db_backend.translate_sql``) ので、プレースホルダは sqlite 形式で書けばよい。
    """
    con = connect()
    try:
        cur = con.cursor()
        placeholders = ",".join(["?"] * len(article_ids))
        cur.execute(
            f"SELECT {ARTICLE_COLS} FROM articles WHERE article_id IN ({placeholders})",
            article_ids,
        )
        return {str(r["article_id"]): r for r in cur.fetchall()}
    finally:
        con.close()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    gold = _load_gold()
    print(f"gold 件数 {len(gold)} (境界除く {sum(1 for g in gold if not g['boundary'])})")

    article_ids = sorted({str(g["article_id"]) for g in gold})
    articles = _fetch_articles(article_ids)
    missing = [a for a in article_ids if a not in articles]
    if missing:
        print(f"⚠ DB に見つからない article_id {len(missing)} 件 (除外): {missing[:5]}...")

    cfg = load_app_config()
    # gold には無効化された PIR も残り得るので全件 (enabled 問わず) を使う
    all_pirs = {p.id: p for p in get_pir_config().priorities}

    llm = build_llm_for_ref(args.model, Step.PIR_LLM_JUDGE, cfg)

    results = []
    judged = failed = 0
    for i, g in enumerate(gold, start=1):
        aid, pid = str(g["article_id"]), str(g["pir_id"])
        row = articles.get(aid)
        pir = all_pirs.get(pid)
        if row is None or pir is None:
            results.append({**g, "pred": None, "skip_reason": "article_or_pir_missing"})
            continue
        try:
            prompt = build_judge_prompt(pir, row)
            v = await llm.generate_structured(
                prompt, JudgeVerdict, temperature=0.0, max_tokens=300, think=False
            )
            judged += 1
            results.append({**g, "pred": v.matched, "reason_pred": v.reason})
        except Exception as exc:  # noqa: BLE001
            failed += 1
            results.append({**g, "pred": None, "error": type(exc).__name__})
        if i % 10 == 0:
            print(f"  {i}/{len(gold)} judged={judged} failed={failed}", flush=True)

    scored = [r for r in results if r["pred"] is not None]
    agree = sum(1 for r in scored if r["pred"] == r["matched"])
    nb = [r for r in scored if not r["boundary"]]
    agree_nb = sum(1 for r in nb if r["pred"] == r["matched"])

    print(f"\nmodel={args.model}")
    print(f"判定成功 {judged}/{len(gold)} (失敗 {failed}、欠損 {len(gold) - judged - failed})")
    print(f"全体一致率: {agree}/{len(scored)}")
    print(f"境界除く一致率: {agree_nb}/{len(nb)} = {agree_nb / max(1, len(nb)):.1%}")

    out_path = (
        Path(args.out)
        if args.out
        else Path(f"data/eval/i58_pir_judge_gold_v2_{args.model.replace(':', '_')}.jsonl")
    )
    with out_path.open("w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"出力: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
