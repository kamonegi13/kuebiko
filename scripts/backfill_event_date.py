#!/usr/bin/env python3
"""event_date が null の既存記事に、ローカル LLM で「事象の実発生(検知/公表)日」を補完する。

時間軸レイヤ b/c (2026-06-27): 取得/報道時刻 (published_at/created_at) と「実発生時刻」を
分離する。summarizer は前方の新記事に event_date/basis/compromise_date を付けるが、
既存バックログは null のため本スクリプトで救済する。

設計 (CLAUDE.md §4 / [[cyber_geopolitical_correlation]]):
  - **ローカル LLM のみ** (Ollama)。外部 API・クラウド送信なし。
  - **保守的**: 本文に明示された日付のみ。推測・年のみ等の曖昧は null
    (偽の event_date は dwell / event-time 分析を汚すため)。検証は src.main._normalize_temporal
    に集約 (報道日 +1d 上限 / 侵害<検知 / basis enum)。
  - **additive only**: 既に event_date がある record は触らない (event_date IS NULL のみ)。
  - 既定は **dry-run**、``--apply`` で初めて UPDATE。``--limit`` で件数制限 (newest 優先)。
    off-hours 推奨 (Ollama 競合回避、cron 時刻を避ける)。

Usage (production = PG、コンテナ内で実行):
    docker exec kuebiko /app/.venv/bin/python3 \\
        /app/scripts/backfill_event_date.py --limit 200            # dry-run
    docker exec kuebiko /app/.venv/bin/python3 \\
        /app/scripts/backfill_event_date.py --limit 200 --apply    # 実行
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config_loader import load_app_config  # noqa: E402
from src.main import _normalize_temporal  # noqa: E402
from src.storage.db_backend import connect  # noqa: E402
from src.tools.llm_client import OllamaClient  # noqa: E402

_PROMPT = """記事から「主要事象の時間軸」を抽出。**本文に明示された日付のみ**、推測禁止。

- event_date (YYYY-MM-DD|null): 主要事象が実際に発生/検知/公表された日。取得日・報道日でない。
  月のみ明示は当月1日。過去事案(関連記事・出典の昔の事件)の日付は本件にしない。曖昧/年のみは null。
- event_date_basis (null可): occurred|detected|disclosed|reported|relative。null なら null。
- compromise_date (YYYY-MM-DD|null): 初期侵害の開始日が明示されていれば (例「3月から侵入」)。
  なければ null。必ず event_date 以前。

記事タイトル: {title}
記事本文:
{body}

JSON {{"event_date","event_date_basis","compromise_date"}} のみ返答。"""


class _Temporal(BaseModel):
    event_date: str | None = None
    event_date_basis: Literal["occurred", "detected", "disclosed", "reported", "relative", None] = (
        None
    )
    compromise_date: str | None = None


def _fetch(limit: int) -> list[tuple[int, str, str, object]]:
    """(id, title, body/summary, published_or_created) を新しい順で返す (event_date=null)。"""
    con = connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT id, title, COALESCE(NULLIF(body,''), summary) AS txt, "
            "COALESCE(published_at, created_at) AS ref_at "
            "FROM articles WHERE status='posted' AND event_date IS NULL "
            "AND COALESCE(NULLIF(body,''), summary) IS NOT NULL "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()
    finally:
        con.close()
    return [(int(r[0]), str(r[1] or ""), str(r[2] or ""), r[3]) for r in rows]


def _ref_date(raw: object) -> date:
    """published/created を date に。事象は報道後に起きえないので +1d を上限に使う。"""
    if isinstance(raw, datetime):
        return raw.date() + timedelta(days=1)
    if isinstance(raw, date):
        return raw + timedelta(days=1)
    if isinstance(raw, str) and len(raw) >= 10:
        try:
            return date.fromisoformat(raw[:10]) + timedelta(days=1)
        except ValueError:
            pass
    return date.today() + timedelta(days=1)


async def _extract_one(
    client: OllamaClient,
    sem: asyncio.Semaphore,
    item: tuple[int, str, str, object],
) -> tuple[int, str, str | None, str | None] | None:
    """1 記事の event_date を抽出して (id, ev, basis, comp) を返す。失敗/未抽出は None。"""
    aid, title, body, pub = item
    prompt = _PROMPT.format(title=title, body=body[:2500])
    async with sem:
        try:
            res = await client.generate_structured(prompt, schema=_Temporal, think=False)
        except Exception:  # noqa: BLE001 — 1 件失敗は skip して継続
            return None
    ev, basis, comp = _normalize_temporal(
        res.event_date, res.event_date_basis, res.compromise_date, reference=_ref_date(pub)
    )
    if not ev:
        return None
    return (aid, ev, basis, comp)


def _apply_chunk(updates: list[tuple[int, str, str | None, str | None]]) -> None:
    """1 チャンク分を additive UPDATE で commit (event_date IS NULL のみ)。"""
    con = connect()
    try:
        cur = con.cursor()
        for aid, ev, basis, comp in updates:
            cur.execute(
                "UPDATE articles SET event_date=?, event_date_basis=?, compromise_date=? "
                "WHERE id=? AND event_date IS NULL",
                (ev, basis, comp, aid),
            )
        con.commit()
    finally:
        con.close()


_CHUNK = 150  # チャンクごとに commit (大規模 overnight 実行の durability + 進捗可視化)


async def main(limit: int, apply: bool, concurrency: int) -> int:
    config = load_app_config()
    client = OllamaClient(
        base_url=config.ollama_base_url,
        model=config.ollama_main_model,
        timeout_seconds=120.0,
    )
    items = _fetch(limit)
    print(
        f"=== event_date LLM 補完: 候補 {len(items)} 記事 "
        f"(event_date=null, 並列 {concurrency}, chunk {_CHUNK}) ==="
    )

    sem = asyncio.Semaphore(concurrency)
    done = dwell_n = 0
    # チャンク単位で抽出→commit。途中クラッシュでも済んだチャンクは残り、
    # 再実行は newest-null-first で続きから (resumable)。
    for start in range(0, len(items), _CHUNK):
        chunk = items[start : start + _CHUNK]
        results = await asyncio.gather(*[_extract_one(client, sem, it) for it in chunk])
        updates = [r for r in results if r is not None]
        dwell_n += sum(1 for _, _, _, comp in updates if comp)
        done += len(updates)
        if apply and updates:
            _apply_chunk(updates)
        print(
            f"  chunk {start // _CHUNK + 1}: +{len(updates)}/{len(chunk)} "
            f"(累計 {done} 解決 / {start + len(chunk)} 処理)"
        )

    print(
        f"\n解決 {done} / skip {len(items) - done} / 候補 {len(items)}"
        f"  (event_date 埋まり {done * 100 // max(len(items), 1)}% / dwell {dwell_n})"
    )
    if not apply:
        print("\n[dry-run] 書き込みなし。--apply で additive UPDATE を実行する。")
    else:
        print(f"[applied] {done} 件 (chunk ごとに commit 済、うち dwell {dwell_n})。")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    lim = 200
    conc = 4  # off-peak 既定。pipeline と重なる時間は下げる
    for i, a in enumerate(args):
        if a == "--limit" and i + 1 < len(args):
            lim = int(args[i + 1])
        elif a == "--concurrency" and i + 1 < len(args):
            conc = max(1, int(args[i + 1]))
    sys.exit(asyncio.run(main(lim, apply="--apply" in args, concurrency=conc)))
