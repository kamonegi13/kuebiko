"""事象ニュースの毎時ジョブ本体 (shadow 運用)。

収集サイクルの後に走り、**前回実行以降に取り込まれた記事のみ**を既存アイテムへ
合流させる。生成はメンバー 2 件以上のアイテムに限る (単独記事は per-article 要約を
そのまま読ませる — docs/event_news_design.md §14b の案 A)。

**この時点では読み手向けの出口を持たない** (UI/Discord 未配線)。目的は
①毎時運用が成立するかの実証 ②毎時の LLM 占有時間の実測 の 2 つ。
``EVENTNEWS_HOURLY=0`` で完全停止できる。
"""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import numpy as np

from src.config_loader import load_app_config
from src.eventnews.grouping import build_join_entities
from src.eventnews.hourly import hydrate_open_items, run_hourly
from src.eventnews.models import MemberArticle
from src.logging_config import get_logger
from src.storage.event_time import DEDUP_ARTICLES, EVENT_TS_EXPR
from src.storage.run_history import RunHistoryRepository
from src.tools.llm_client import LLMClient
from src.tools.model_tiers import Step, build_llm_for

_log = get_logger(__name__)

_FLAG = "EVENTNEWS_HOURLY"
# 候補の取込窓。毎時実行なら 1 時間で足りるが、実行が飛んだ場合の取りこぼしを防ぐため
# 広めに取る (既にメンバーの記事は hourly.run_hourly が候補から外すので冪等)。
_CANDIDATE_LOOKBACK_HOURS = 6
_TS = EVENT_TS_EXPR.format(a="a")

_SQL_CANDIDATES = f"""
SELECT a.article_id, {_TS} AS anchor_ts, x.importance, x.category, x.status,
       x.title, x.url, x.feed_title, x.feed_url, x.summary, x.body
FROM {DEDUP_ARTICLES} a
JOIN (
  SELECT article_id, MAX(importance) importance, MAX(category) category,
         MAX(status) status, MAX(title) title, MAX(url) url,
         MAX(COALESCE(feed_title,'')) feed_title, MAX(COALESCE(feed_url,'')) feed_url,
         MAX(COALESCE(summary,'')) summary, MAX(COALESCE(body,'')) body
  FROM articles GROUP BY article_id
) x ON x.article_id = a.article_id
WHERE a.created_at >= ? AND x.importance IN ('high','medium')
  AND x.status IN ('posted','skipped_duplicate')
ORDER BY 2
"""

_SQL_ENTITIES = """
SELECT article_id, entity_type, LOWER(TRIM(value)) FROM article_entities
WHERE entity_type IN ('cve','victim_org','actor','malware_family')
  AND LENGTH(TRIM(value)) >= 4 AND created_at >= ?
"""


def _to_member(row: Sequence[object], entities: frozenset[tuple[str, str]]) -> MemberArticle:
    aid, ts, imp, cat, status, title, url, feed_title, feed_url, summary, body = row
    anchor = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts))
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=UTC)
    return MemberArticle(
        article_id=str(aid),
        title=str(title),
        url=str(url),
        feed_title=str(feed_title),
        feed_url=str(feed_url),
        host=str(url).split("/")[2] if "://" in str(url) else "",
        importance=str(imp),
        category=str(cat),
        status=str(status),
        anchor_ts=anchor,
        summary=str(summary),
        body=str(body),
        entities=entities,
    )


async def run_eventnews_hourly() -> dict[str, object]:
    """毎時ジョブの入口。戻り値は run_history に載せる要約。"""
    if os.environ.get(_FLAG, "1") == "0":
        _log.info("eventnews_hourly_disabled")
        return {"skipped": "flag_off"}

    started = time.monotonic()
    repo = RunHistoryRepository()
    since = datetime.now(UTC) - timedelta(hours=_CANDIDATE_LOOKBACK_HOURS)

    with repo._connect() as conn:  # noqa: SLF001 — repo 内部接続の再利用 (他ジョブと同型)
        rows = conn.execute(_SQL_CANDIDATES, (since.isoformat(),)).fetchall()
        ent_rows = conn.execute(_SQL_ENTITIES, (since.isoformat(),)).fetchall()

    counts: dict[tuple[str, str], int] = {}
    raw = [(str(r[0]), str(r[1]), str(r[2])) for r in ent_rows]
    for _, etype, value in raw:
        counts[(etype, value)] = counts.get((etype, value), 0) + 1
    join_ents = build_join_entities(raw, counts)
    candidates = [_to_member(r, join_ents.get(str(r[0]), frozenset())) for r in rows]

    vectors = _load_vectors(repo, [c.article_id for c in candidates])
    candidates = [c for c in candidates if c.article_id in vectors]
    if not candidates:
        _log.info("eventnews_hourly_no_candidates")
        return {"candidates": 0, "elapsed_seconds": round(time.monotonic() - started, 1)}

    existing = hydrate_open_items(repo, lambda ids: _load_members(repo, ids, join_ents))
    for _, members in existing:
        vectors.update(_load_vectors(repo, [m.article_id for m in members]))

    config = load_app_config()

    def _llm() -> LLMClient:
        return build_llm_for(Step.EVENT_NEWS, config)

    result = await run_hourly(repo, candidates, vectors, existing, _llm)
    elapsed = round(time.monotonic() - started, 1)
    _log.info(
        "eventnews_hourly_summary",
        elapsed_seconds=elapsed,
        candidates=result.candidates,
        hydrated=result.hydrated_items,
        generated=result.stats.generated,
    )
    return {
        "candidates": result.candidates,
        "hydrated_items": result.hydrated_items,
        "created": result.stats.created,
        "updated": result.stats.updated,
        "reinforced": result.stats.reinforced,
        "generated": result.stats.generated,
        "elapsed_seconds": elapsed,
    }


def _load_vectors(repo: RunHistoryRepository, article_ids: list[str]) -> dict[str, np.ndarray]:
    """記事 URL 経由で埋込を引く (article_embeddings は url_hash / url がキー)。"""
    if not article_ids:
        return {}
    out: dict[str, np.ndarray] = {}
    placeholders = ",".join("?" for _ in article_ids)
    with repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            "SELECT a.article_id, e.vector, e.dim FROM articles a"  # noqa: S608 — placeholders のみ
            f" JOIN article_embeddings e ON e.url = a.url WHERE a.article_id IN ({placeholders})",
            article_ids,
        ).fetchall()
    for aid, vec, dim in rows:
        v = np.frombuffer(bytes(vec), dtype=np.float32)
        if v.shape[0] == int(dim):
            out[str(aid)] = v / np.linalg.norm(v)
    return out


def _load_members(
    repo: RunHistoryRepository,
    article_ids: list[str],
    join_ents: dict[str, frozenset[tuple[str, str]]],
) -> dict[str, MemberArticle]:
    if not article_ids:
        return {}
    placeholders = ",".join("?" for _ in article_ids)
    with repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            f"SELECT a.article_id, {EVENT_TS_EXPR.format(a='a')}, a.importance, a.category,"  # noqa: S608
            " a.status, a.title, a.url, COALESCE(a.feed_title,''), COALESCE(a.feed_url,''),"
            " COALESCE(a.summary,''), COALESCE(a.body,'') FROM articles a"
            f" WHERE a.article_id IN ({placeholders})",
            article_ids,
        ).fetchall()
    return {str(r[0]): _to_member(r, join_ents.get(str(r[0]), frozenset())) for r in rows}


__all__ = ["run_eventnews_hourly"]
