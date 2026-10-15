"""重複 alert の ML を「影として記録だけ」する配信時点の口 (R1h、2026-10-11)。

alert 降格の判定 (``alert_demotion.maybe_demote_alert``) の直前に呼ぶ。**配信先・msg は
一切変えない** (戻り値は記録したかどうかだけ)。先行 alert が見つからなければ行を作らない。
例外は握りつぶさず log し、配信は続ける。flag ``ALERT_DEDUP_SHADOW`` (既定 on、"0" で停止)。
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import numpy as np

from src.cti.alert_dedup_features import (
    FEATURE_NAMES,
    AlertSide,
    alert_dedup_features,
    select_prior,
)
from src.cti.alert_dedup_gate import (
    ALERT_CHANNEL,
    ALERT_DEDUP_LOOKBACK_HOURS,
    EMBEDDING_COS_THRESHOLD,
    build_current_entities,
)
from src.cti.alert_dedup_model import load_alert_dedup_model
from src.eventnews.pair_model import load_model
from src.logging_config import get_logger
from src.storage.repo_alert_dedup_shadow import AlertDedupShadowRow

if TYPE_CHECKING:
    from src.storage.run_history import RunHistoryRepository
    from src.tools.article_model import Article
    from src.tools.discord_publisher import BriefingMessage

_log = get_logger(__name__)

SHADOW_ENV = "ALERT_DEDUP_SHADOW"
#: 候補検索の上限件数 (alert_dedup_gate と同じ)
_CANDIDATE_TOP_K = 30


def is_alert_dedup_shadow_enabled() -> bool:
    """既定 on。"0" (または空) を明示したときだけ止まる。"""
    return os.environ.get(SHADOW_ENV, "1").strip() not in ("", "0")


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _load_candidates(
    repo: RunHistoryRepository, vector: list[float], embedding_model: str
) -> list[AlertSide]:
    """過去 72 時間に alert で投稿済みの記事のうち、cos 候補になるものを AlertSide にする。"""
    similar = repo.find_similar_embeddings(
        vector,
        model=embedding_model,
        top_k=_CANDIDATE_TOP_K,
        threshold=EMBEDDING_COS_THRESHOLD,
        window_hours=ALERT_DEDUP_LOOKBACK_HOURS,
    )
    records = repo.get_articles_by_urls([c[1] for c in similar])
    out: list[AlertSide] = []
    for hash_val, cand_url, _sim in similar:
        rec = records.get(cand_url)
        if rec is None or rec.posted_channel != ALERT_CHANNEL or rec.status != "posted":
            continue
        prior_vec = repo.get_embedding(hash_val)
        if prior_vec is None:
            continue
        out.append(
            AlertSide(
                article_id=rec.article_id,
                title=rec.title or "",
                summary=rec.summary or "",
                category=rec.category or "",
                feed_title=rec.feed_title or "",
                feed_url=rec.feed_url or "",
                account_class=rec.account_class or "",
                time=_aware(rec.created_at),
                entities=frozenset(repo.get_article_entities(rec.article_id)),
                vector=np.asarray(prior_vec, dtype=np.float32),
            )
        )
    return out


def _current_side(
    msg: BriefingMessage,
    article: Article | None,
    article_id: str,
    vector: list[float],
    asof: datetime,
) -> AlertSide:
    meta = msg.metadata
    entities = build_current_entities(
        iocs=msg.iocs,
        mitre_techniques=msg.mitre_techniques,
        malware_families=list(meta.get("malware_families") or []),
        tools=list(meta.get("tools") or []),
        detected_actor_ids=list(meta.get("detected_actor_ids") or []),
        victim_orgs=list(meta.get("victim_orgs") or []),
    )
    return AlertSide(
        article_id=article_id,
        title=msg.title or "",
        summary=msg.summary or "",
        category=msg.category or "",
        feed_title=article.feed_title if article else "",
        feed_url=article.feed_url if article else "",
        account_class=article.account_class if article else "",
        time=asof,
        entities=entities,
        vector=np.asarray(vector, dtype=np.float32),
    )


def _skip(article_id: str, reason: str) -> bool:
    """記録しなかった理由を残す (0 行の原因が追えるように)。常に False。"""
    _log.info("alert_dedup_shadow_skipped", article_id=article_id, reason=reason)
    return False


def record_alert_dedup_shadow(
    *,
    msg: BriefingMessage,
    article: Article | None,
    article_id: str,
    embedding: tuple[str, list[float]] | None,
    repo: RunHistoryRepository | None,
    asof: datetime | None = None,
) -> bool:
    """影の記録を 1 行追記する。記録したら True。配信には一切影響しない (例外も外へ出さない)。"""
    if not is_alert_dedup_shadow_enabled():
        return False
    if repo is None:
        return _skip(article_id, "no_repo")
    if embedding is None:
        return _skip(article_id, "no_embedding")
    try:
        pair_model, model = load_model(), load_alert_dedup_model()
        if pair_model is None or model is None:
            return _skip(article_id, "model_missing")
        embedding_model, vector = embedding
        cur = _current_side(msg, article, article_id, vector, asof or datetime.now(UTC))
        choice = select_prior(cur, _load_candidates(repo, vector, embedding_model), pair_model)
        if choice is None:
            return _skip(article_id, "no_prior_alert")
        features = alert_dedup_features(cur, choice.side, choice.p_pair)
        p = model.p_suppress(features)
        repo.record_alert_dedup_shadow(
            AlertDedupShadowRow(
                article_id=article_id,
                prior_article_id=choice.side.article_id,
                p_suppress=p,
                model_version=model.version,
                features_json=json.dumps(
                    dict(zip(FEATURE_NAMES, features, strict=True)), ensure_ascii=False
                ),
                would_suppress=model.would_suppress(p),
            )
        )
        return True
    except Exception as e:  # noqa: BLE001 — 影の記録の失敗で配信を止めない (log して続ける)
        _log.warning("alert_dedup_shadow_failed", article_id=article_id, error=str(e)[:200])
        return False


__all__ = ["SHADOW_ENV", "is_alert_dedup_shadow_enabled", "record_alert_dedup_shadow"]
