#!/usr/bin/env python3
"""BOUNDARY_RULES ブロックの A/B 実験 (読み取り専用実験・本番 DB/config は変更しない)。

Arm A = 本番の triage prompt (PIR-driven、DB config_store 現行値 + 当該記事の最新 DB row)。
Arm B = Arm A + ``data/mlx/adjudicate_gold.py`` の ``BOUNDARY_RULES`` (A-I の境界の型の定め、
2026-10-03 決定) を末尾にそのまま追記 (同じ位置、同じ文字列 — adjudicate_gold.py の
``triage_boundary_items()`` と同じ合成)。

対象: data/eval/triage_gold_v4.jsonl ∪ triage_gold_v2.jsonl (実測では同一 150 件)。
記事本文は articles テーブルから article_id 最新行 (今回は全件 1 行のみ) を読む。
DB の body が空/NULL の場合のみ triage_goldset_v2.json の summary へ fallback する。

実行は i58 (本番 fast ティア、config_store の model_tiers が指す実モデル) を
``build_llm_for(Step.TRIAGE, load_app_config())`` で解決し、本番と同じ
schema (TriageDecision) / think=False / max_tokens=400 で呼ぶ。並列度 1 (直列)。

再開可能: 既に書き出し済みの (article_id, arm) は skip する。
朝夕ブリーフ (06:30 / 19:30 JST ±15 分) と、ジョブAPIが running を返す間は一時停止する
(本番の LLM 枠を奪わない)。

出力: data/eval/triage_boundary_ab_v1.jsonl (追記、1 行 = 1 記事 × 1 arm)。
書き込み先はこのファイルのみ。config_store / articles は読み取りのみ。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/data/mlx")

from adjudicate_gold import BOUNDARY_RULES  # noqa: E402  (data/mlx、同じ定数をそのまま使う)

from src.config_loader import load_app_config  # noqa: E402
from src.pir.integration import (  # noqa: E402
    build_triage_high_criteria,
    build_triage_medium_criteria,
    get_pir_config,
)
from src.storage.db_backend import connect  # noqa: E402
from src.tools.article_model import Article  # noqa: E402
from src.tools.article_triage import ArticleTriage, TriageDecision  # noqa: E402
from src.tools.model_tiers import Step, build_llm_for  # noqa: E402

EVAL = Path("/app/data/eval")
OUT = Path(os.environ.get("BOUNDARY_AB_OUT", str(EVAL / "triage_boundary_ab_v1.jsonl")))  # noqa: E501
GOLD_V4 = EVAL / "triage_gold_v4.jsonl"
GOLD_V2 = EVAL / "triage_gold_v2.jsonl"
GOLDSET_SRC = EVAL / "triage_goldset_v2.json"  # title/feed/summary の fallback 元

JST = timezone(timedelta(hours=9))
JOBS_URL = "http://host.docker.internal:8001/api/v1/jobs"
BRIEF_CENTERS_MIN = (6 * 60 + 30, 19 * 60 + 30)  # 06:30 / 19:30 JST
BRIEF_WINDOW_MIN = 15
POLL_SLEEP_SEC = 60


def _load_gold_keys() -> list[str]:
    keys: set[str] = set()
    for path in (GOLD_V4, GOLD_V2):
        for line in path.open(encoding="utf-8"):
            keys.add(json.loads(line)["key"])
    return sorted(keys)


def _load_goldset_fallback() -> dict[str, dict[str, str]]:
    rows = json.loads(GOLDSET_SRC.read_text(encoding="utf-8"))["rows"]
    return {r["article_id"]: r for r in rows}


def _done_keys() -> set[tuple[str, str]]:
    if not OUT.exists():
        return set()
    done: set[tuple[str, str]] = set()
    for line in OUT.open(encoding="utf-8"):
        r = json.loads(line)
        done.add((r["article_id"], r["arm"]))
    return done


def _fetch_latest_row(
    cur: object, article_id: str
) -> tuple[str | None, str | None, str | None] | None:
    cur.execute(  # type: ignore[attr-defined]
        "select title, feed_title, body from articles where article_id = ? "
        "order by created_at desc limit 1",
        (article_id,),
    )
    return cur.fetchone()  # type: ignore[attr-defined,no-any-return]


def _build_article(article_id: str, row: tuple | None, fallback: dict[str, str]) -> Article:
    title = (row[0] if row else None) or fallback.get("title", "") or article_id
    feed_title = (row[1] if row else None) or fallback.get("feed", "")
    body_text = (row[2] if row else None) or fallback.get("summary", "") or None
    return Article(
        id=article_id,
        title=title,
        url="https://kuebiko.example/boundary-ab",
        summary_html="",
        published=datetime.now(UTC),
        feed_title=feed_title,
        feed_url="",
        body_text=body_text,
    )


def _in_brief_blackout(now_jst: datetime) -> bool:
    hm = now_jst.hour * 60 + now_jst.minute
    return any(abs(hm - center) <= BRIEF_WINDOW_MIN for center in BRIEF_CENTERS_MIN)


#: 一時停止の対象は朝夕ブリーフそのもの (heavy、GPU 専有が長い) のみ。毎時収集系まで
#: 対象にすると hourly-collect/-upkeep が常時 running で実験が進まなくなるため絞る
#: (CLAUDE.md 2026-09-27 「評価は本番コンテナの中で回さない」の精神 — GPU 取り合いを避ける
#: べき対象は heavy chain、毎時の軽量ジョブとは独立に 1 並列実行で共存できる)。
_BRIEF_JOB_IDS = ("morning-brief", "evening-brief")


def _job_chain_running() -> bool:
    try:
        with urllib.request.urlopen(JOBS_URL, timeout=5) as resp:  # noqa: S310
            data = json.load(resp)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        # 読み取り専用実験のため、死活確認が届かないこと自体では止めない (fail-open)。
        return False
    for job in data.get("jobs", []):
        if job.get("id") not in _BRIEF_JOB_IDS:
            continue
        last_run = job.get("last_run") or {}
        if last_run.get("status") == "running":
            return True
        if job.get("running_since"):
            return True
    return False


async def _wait_while_blacked_out() -> None:
    while True:
        now_jst = datetime.now(JST)
        blackout = _in_brief_blackout(now_jst)
        running = _job_chain_running()
        if not blackout and not running:
            return
        reason = "brief-window" if blackout else "job-running"
        print(f"pause ({reason}) at {now_jst.isoformat()}, sleeping {POLL_SLEEP_SEC}s", flush=True)
        await asyncio.sleep(POLL_SLEEP_SEC)


async def main() -> None:
    import os

    keys = _load_gold_keys()
    limit = os.environ.get("BOUNDARY_AB_LIMIT")
    if limit:
        keys = keys[: int(limit)]
    fallback_by_id = _load_goldset_fallback()
    print(f"articles: {len(keys)}", flush=True)

    conn = connect()
    cur = conn.cursor()
    articles: dict[str, Article] = {}
    db_empty = 0
    for aid in keys:
        row = _fetch_latest_row(cur, aid)
        if row is None or not row[2]:
            db_empty += 1
        articles[aid] = _build_article(aid, row, fallback_by_id.get(aid, {}))
    conn.close()
    print(f"DB body 空/欠落 (goldset summary へ fallback): {db_empty}", flush=True)

    cfg = load_app_config()
    llm = build_llm_for(Step.TRIAGE, cfg)
    pir_cfg = get_pir_config()
    high_criteria = build_triage_high_criteria(pir_cfg.priorities)
    medium_criteria = build_triage_medium_criteria(pir_cfg.priorities)
    if not high_criteria:
        print(
            "WARNING: high_criteria が空 (PIR-driven triage が無効化されている可能性)", flush=True
        )
    triage = ArticleTriage(llm, think=False)

    done = _done_keys()
    total_calls = len(keys) * 2
    made = 0
    with OUT.open("a", encoding="utf-8") as fh:
        for aid in keys:
            article = articles[aid]
            prompt_a = triage._build_prompt_pir_driven(article, high_criteria, medium_criteria)  # noqa: SLF001
            prompt_b = prompt_a + BOUNDARY_RULES
            arms = os.environ.get("BOUNDARY_AB_ARMS", "AB")
            for arm, prompt in (("A", prompt_a), ("B", prompt_b)):
                if arm not in arms:
                    continue
                made += 1
                if (aid, arm) in done:
                    continue
                await _wait_while_blacked_out()
                try:
                    decision = await llm.generate_structured(
                        prompt, schema=TriageDecision, think=False, max_tokens=400
                    )
                    rec = {
                        "article_id": aid,
                        "arm": arm,
                        "importance": decision.importance,
                        "reason": decision.reason,
                        "llm_error": decision.error,
                        "title": article.title,
                        "feed": article.feed_title,
                        "model": llm.model,
                    }
                except Exception as e:  # noqa: BLE001 — 本番と同じ fail-open (medium) で記録を続ける
                    rec = {
                        "article_id": aid,
                        "arm": arm,
                        "importance": "medium",
                        "reason": f"error:{type(e).__name__}:{str(e)[:150]}",
                        "llm_error": True,
                        "title": article.title,
                        "feed": article.feed_title,
                        "model": getattr(llm, "model", "?"),
                    }
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                if made % 20 == 0:
                    print(f"{made}/{total_calls}", flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
