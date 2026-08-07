"""判断分類器の ground-truth 目視評価 (2026-07-27)。

「26B との一致率」でなく **記事本文に照らした正誤** を判定するためのダンプ。各記事の
本文抜粋 + 各モデルの判断出力を並べ、人が本文を読んでどのモデルが正しいかを判定する。
gpt-oss-safeguard は出力ゼロのため除外、31B は遅延失格のため除外。viable な 26B/nemotron/
mistral/gemma3 を比較する。

usage:
    uv run python scripts/dump_judgment_groundtruth.py --n 8
"""

from __future__ import annotations

import argparse
import asyncio

from src.config_loader import load_app_config
from src.cti.actor_normalizer import load_actor_aliases
from src.cti.judgment_classifier import classify_judgment
from src.pipeline.persistence import _relevant_actors
from src.storage.run_history import RunHistoryRepository
from src.tools.model_tiers import Step, build_llm_for, build_llm_for_ref

_CYBER = ("apt", "malware", "incident", "breach", "advisory", "phishing", "vulnerability")
_MODELS = {
    "26B": None,  # baseline (build_llm_for) = gemma4:26b
    # 追加モデルは ref を書けば比較できる (例: "31b": "gemma4:31b")。
    # 2026-07-27 に中華系 (glm-4.7-flash / qwen3.5:35b) を計測した際は中立名 variant
    # (xmodela/xmodelb) を作り temp0+正しい chat template で再パッケージした。結果: GLM は
    # 26B と互角・Qwen は recap 誤分類で劣る。§4 により本番不可のため計測後に全削除済み。
}


async def dump(n: int) -> None:
    cfg = load_app_config()
    repo = RunHistoryRepository()
    registry = load_actor_aliases()
    llms = {
        name: (build_llm_for(Step.ARTICLE_SUMMARY, cfg) if ref is None
               else build_llm_for_ref(ref, Step.ARTICLE_SUMMARY, cfg))
        for name, ref in _MODELS.items()
    }
    cyber_ph = ",".join("?" for _ in _CYBER)
    sql = (
        "SELECT a.article_id, a.title, a.body, a.category, MAX(a.created_at) ts "
        "FROM articles a "
        f"WHERE a.category IN ({cyber_ph}) AND a.body IS NOT NULL AND a.body <> '' "
        "AND a.feed_url NOT LIKE ? "
        "GROUP BY a.article_id, a.title, a.body, a.category "
        "ORDER BY ts DESC LIMIT ?"
    )
    with repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute(sql, (*_CYBER, "%ransomware.live%", n)).fetchall()

    for i, r in enumerate(rows, 1):
        title = str(r["title"] or "")
        body = str(r["body"] or "")
        category = str(r["category"] or "") or None
        cands = _relevant_actors(registry.find_all(body), category)
        print(f"\n{'=' * 90}")
        print(f"[{i}] {title}")
        print(f"    取込cat={category} 言及候補=[{','.join(c.id for c in cands) or '-'}]")
        print(f"    本文抜粋: {' '.join(body.split())[:280]}")
        print(f"    {'model':8s} {'art_type':10s} {'stance':16s} {'intent':14s} i_infra subj")
        for name, llm in llms.items():
            try:
                j = await classify_judgment(
                    llm, title=title, category=category, body=body,
                    published=None, candidates=cands,
                )
            except Exception as e:  # noqa: BLE001
                print(f"    {name:8s} ERR {str(e)[:40]}")
                continue
            if j is None:
                print(f"    {name:8s} (None)")
                continue
            print(
                f"    {name:8s} {j.article_type:10s} {j.editorial_stance:16s} "
                f"{j.intent:14s} {str(j.i_infra):5s} {j.subject_actor_id or '-'}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=8)
    args = parser.parse_args()
    asyncio.run(dump(args.n))


if __name__ == "__main__":
    main()
