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
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

import numpy as np

from src.config_loader import load_app_config
from src.eventnews.grouping import build_join_entities
from src.eventnews.hourly import hydrate_open_items, run_hourly
from src.eventnews.models import ENTITY_FREQ_WINDOW_HOURS, MemberArticle
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

# entity は **article_id で引く**。``article_entities.created_at`` は行を書いた時刻で
# あって事象時刻ではない (バックフィル・再抽出は過去記事へ当日の日付を書く)。
# ここを時刻で絞ると、復元した既存メンバー (最大 72h 前) の entity が空になり、
# 「共有 entity >= 1」が永久に不成立 → **合流が構造的に起きなくなる**
# (2026-08-24: 本番で 25 アイテム全件が単独記事のままだった原因)。
_SQL_ENTITIES_BY_ID = """
SELECT article_id, entity_type, LOWER(TRIM(value)) FROM article_entities
WHERE entity_type IN ('cve','victim_org','actor','malware_family')
  AND LENGTH(TRIM(value)) >= 4 AND article_id IN ({placeholders})
"""

# 頻出ガード (ENTITY_FREQ_CAP) の分母。窓内コーパス全体で数えないと、
# 手元の数十件では cap に届かず頻出語が結合信号として通ってしまう。
_SQL_ENTITY_COUNTS = f"""
SELECT e.entity_type, LOWER(TRIM(e.value)), COUNT(DISTINCT e.article_id)
FROM article_entities e
WHERE e.entity_type IN ('cve','victim_org','actor','malware_family')
  AND LENGTH(TRIM(e.value)) >= 4
  AND e.article_id IN (
    SELECT a.article_id FROM {DEDUP_ARTICLES} a WHERE a.created_at >= ?
  )
GROUP BY 1, 2
"""


def _dedupe_by_url(candidates: Sequence[MemberArticle]) -> list[MemberArticle]:
    """同一 URL の候補を 1 件に畳む (最も古い錨時刻を残す)。

    ``DEDUP_ARTICLES`` は article_id の重複行を畳むが、**同一 URL に別の article_id が
    付く**ことは畳めない。Grok は同じツイートが別レポートに現れると別 sub-article として
    取り込まれるため (実測: 同一 URL で article_id が複数ある URL 228 件)、そのまま
    候補にすると同じツイートが 1 つの事象へ 2 回入り、メンバー数と「統合した記事数」を
    水増しする (実測: 47 アイテムで余分なメンバー 54 件)。
    """
    seen: dict[str, MemberArticle] = {}
    for c in candidates:
        key = c.url.strip()
        current = seen.get(key)
        if current is None or c.anchor_ts < current.anchor_ts:
            seen[key] = c
    return sorted(seen.values(), key=lambda m: m.anchor_ts)


def _entity_counts(
    repo: RunHistoryRepository, window_start: datetime
) -> dict[tuple[str, str], int]:
    """窓内コーパスでの (entity_type, value) 出現記事数 = 頻出ガードの分母。"""
    with repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute(_SQL_ENTITY_COUNTS, (window_start.isoformat(),)).fetchall()
    return {(str(r[0]), str(r[1])): int(r[2]) for r in rows}


def _join_entities_for(
    repo: RunHistoryRepository,
    article_ids: Sequence[str],
    counts: Mapping[tuple[str, str], int],
) -> dict[str, frozenset[tuple[str, str]]]:
    """指定記事の結合信号 entity を引く (候補・既存メンバーで共通に使う唯一の口)。"""
    if not article_ids:
        return {}
    placeholders = ",".join("?" for _ in article_ids)
    with repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            _SQL_ENTITIES_BY_ID.format(placeholders=placeholders),  # noqa: S608 — placeholders のみ
            list(article_ids),
        ).fetchall()
    return build_join_entities([(str(r[0]), str(r[1]), str(r[2])) for r in rows], counts)


def _to_member(row: Mapping[str, object], entities: frozenset[tuple[str, str]]) -> MemberArticle:
    """行 → MemberArticle。**位置でなくキー名で読む** — 列順・列数の変更に強い。"""
    aid = row["article_id"]
    ts = row["anchor_ts"]
    imp, cat, status = row["importance"], row["category"], row["status"]
    title, url = row["title"], row["url"]
    feed_title, feed_url = row["feed_title"], row["feed_url"]
    summary, body = row["summary"], row["body"]
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
    return await run_eventnews_window(lookback_hours=_CANDIDATE_LOOKBACK_HOURS)


async def run_eventnews_window(*, lookback_hours: int, generate: bool = True) -> dict[str, object]:
    """指定した遡及幅で群化 (+ 生成) を 1 回走らせる。

    毎時ジョブとバックフィルで **取得を完全に共有する** ための唯一の入口。
    2026-08-24 の不発は「評価と本番で entity の引き方が違った」ことが原因だったので、
    遡及幅だけを引数にして、それ以外の経路を分岐させない。
    """
    started = time.monotonic()
    repo = RunHistoryRepository()
    since = datetime.now(UTC) - timedelta(hours=lookback_hours)

    with repo._connect() as conn:  # noqa: SLF001 — repo 内部接続の再利用 (他ジョブと同型)
        rows = conn.execute(_SQL_CANDIDATES, (since.isoformat(),)).fetchall()

    counts = _entity_counts(repo, datetime.now(UTC) - timedelta(hours=ENTITY_FREQ_WINDOW_HOURS))
    cand_ids = [str(r["article_id"]) for r in rows]
    cand_ents = _join_entities_for(repo, cand_ids, counts)
    candidates = [_to_member(r, cand_ents.get(str(r["article_id"]), frozenset())) for r in rows]

    # 既にどこかのアイテムのメンバーになっている記事は候補から外す。run_hourly は
    # **復元したアイテム**のメンバーしか除外できないため、復元窓より古いアイテムの
    # メンバーが再候補化すると決定論の item_id が衝突する (遡及構築で実際に落ちた)。
    already = repo.existing_member_article_ids([c.article_id for c in candidates])
    candidates = [c for c in candidates if c.article_id not in already]
    candidates = _dedupe_by_url(candidates)

    vectors = _load_vectors(repo, [c.article_id for c in candidates])
    candidates = [c for c in candidates if c.article_id in vectors]
    if not candidates:
        _log.info("eventnews_hourly_no_candidates")
        return {"candidates": 0, "elapsed_seconds": round(time.monotonic() - started, 1)}

    existing = hydrate_open_items(repo, lambda ids: _load_members(repo, ids, counts))
    for _, members in existing:
        vectors.update(_load_vectors(repo, [m.article_id for m in members]))

    config = load_app_config()

    def _llm() -> LLMClient:
        return build_llm_for(Step.EVENT_NEWS, config)

    result = await run_hourly(repo, candidates, vectors, existing, _llm if generate else None)
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
    counts: Mapping[tuple[str, str], int],
) -> dict[str, MemberArticle]:
    """既存アイテムのメンバーを本文 + 結合信号 entity ごと復元する。"""
    if not article_ids:
        return {}
    join_ents = _join_entities_for(repo, article_ids, counts)
    placeholders = ",".join("?" for _ in article_ids)
    with repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            # ⚠ **全列に別名を付ける**: PG は dict 形式で行を返すため、別名の無い
            # COALESCE(...) は 4 列とも同じキーへ潰れ、11 列のはずが 8 列になる
            # (2026-08-24 の実障害: 既存アイテムの復元経路だけが落ちた)。
            f"SELECT a.article_id AS article_id,"  # noqa: S608
            f" {EVENT_TS_EXPR.format(a='a')} AS anchor_ts,"
            " a.importance AS importance, a.category AS category, a.status AS status,"
            " a.title AS title, a.url AS url,"
            " COALESCE(a.feed_title,'') AS feed_title,"
            " COALESCE(a.feed_url,'') AS feed_url,"
            " COALESCE(a.summary,'') AS summary,"
            " COALESCE(a.body,'') AS body"
            " FROM articles a"
            f" WHERE a.article_id IN ({placeholders})",
            article_ids,
        ).fetchall()
    return {
        str(r["article_id"]): _to_member(r, join_ents.get(str(r["article_id"]), frozenset()))
        for r in rows
    }


__all__ = ["run_eventnews_hourly"]
