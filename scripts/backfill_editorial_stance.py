#!/usr/bin/env python3
"""editorial_stance が null の記事に、**focused 単機能分類器** で編集スタンスを補完する。

背景 (2026-06-28): editorial_stance は summarizer の routing_flags 内 (free dict) にあり、
prompt 肥大で 26B が脱落させ直近 96% null に。診断で **full summarizer は過負荷で geopolitical を
unknown 連発、focused 単機能分類器なら 10/10 安定** と判明。本スクリプトで null backlog を救済。

設計 (CLAUDE.md §4 / [[trust_layer_s1_s4]]):
  - **ローカル LLM のみ** (Ollama)。外部送信なし。source 名でなく内容(rhetorical 性質)で判定。
  - **additive only**: editorial_stance IS NULL のみ。unknown 判定は書かない (null のまま=未補完)。
  - 既定 **dry-run**、``--apply`` で UPDATE。``--limit``/``--concurrency``。chunk commit。

Usage (コンテナ内):
    docker exec kuebiko /app/.venv/bin/python3 \\
        /app/scripts/backfill_editorial_stance.py --limit 300            # dry-run
    docker exec kuebiko /app/.venv/bin/python3 \\
        /app/scripts/backfill_editorial_stance.py --limit 300 --apply    # 実行
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config_loader import load_app_config  # noqa: E402
from src.cti.editorial_stance_classifier import classify_editorial_stance  # noqa: E402
from src.storage.db_backend import connect  # noqa: E402
from src.tools.llm_client import OllamaClient  # noqa: E402

_CHUNK = 150


def _fetch(limit: int) -> list[tuple[int, str, str]]:
    con = connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT id, title, COALESCE(NULLIF(body,''), summary) AS txt FROM articles "
            "WHERE status='posted' AND (editorial_stance IS NULL OR editorial_stance='') "
            "AND COALESCE(NULLIF(body,''), summary) IS NOT NULL "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()
    finally:
        con.close()
    return [(int(r[0]), str(r[1] or ""), str(r[2] or "")) for r in rows]


async def _classify(
    client: OllamaClient, sem: asyncio.Semaphore, item: tuple[int, str, str]
) -> tuple[int, str] | None:
    aid, title, body = item
    async with sem:
        stance = await classify_editorial_stance(client, title, body)
    # unknown は補完しない (null のまま= 再試行余地)。pipeline と同一実装を共用 (DRY)。
    if stance == "unknown":
        return None
    return (aid, stance)


def _apply(updates: list[tuple[int, str]]) -> None:
    con = connect()
    try:
        cur = con.cursor()
        for aid, stance in updates:
            cur.execute(
                "UPDATE articles SET editorial_stance=? "
                "WHERE id=? AND (editorial_stance IS NULL OR editorial_stance='')",
                (stance, aid),
            )
        con.commit()
    finally:
        con.close()


async def main(limit: int, apply: bool, concurrency: int) -> int:
    cfg = load_app_config()
    client = OllamaClient(
        base_url=cfg.ollama_base_url, model=cfg.ollama_main_model, timeout_seconds=120.0
    )
    items = _fetch(limit)
    print(f"=== editorial_stance focused 補完: 候補 {len(items)} 件 (null, 並列 {concurrency}) ===")
    sem = asyncio.Semaphore(concurrency)
    done = 0
    dist: dict[str, int] = {}
    for start in range(0, len(items), _CHUNK):
        chunk = items[start : start + _CHUNK]
        results = await asyncio.gather(*[_classify(client, sem, it) for it in chunk])
        updates = [r for r in results if r is not None]
        for _, stance in updates:
            dist[stance] = dist.get(stance, 0) + 1
        if apply and updates:
            _apply(updates)
        done += len(updates)
        print(f"  chunk {start // _CHUNK + 1}: +{len(updates)}/{len(chunk)} (累計 {done})")
    print(f"\n解決 {done} / 候補 {len(items)} ({done * 100 // max(len(items), 1)}%)  分布: {dist}")
    if not apply:
        print("[dry-run] 書き込みなし。--apply で UPDATE。")
    else:
        print(f"[applied] {done} 件 (chunk commit 済)。")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    lim, conc = 300, 4
    for i, a in enumerate(args):
        if a == "--limit" and i + 1 < len(args):
            lim = int(args[i + 1])
        elif a == "--concurrency" and i + 1 < len(args):
            conc = max(1, int(args[i + 1]))
    sys.exit(asyncio.run(main(lim, apply="--apply" in args, concurrency=conc)))
