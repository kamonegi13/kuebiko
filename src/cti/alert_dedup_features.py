"""重複 alert の ML 用の特徴量 (R1h、2026-10-11)。配信時点の情報だけで作る純粋ロジック。

GPU / LLM を使わない。**学習 (scripts/train_alert_dedup_model.py) と影の記録
(src/cti/alert_dedup_shadow.py) が同じ関数を呼ぶ** — train/serve の食い違いを作らない。
材料は「今回」と「先行」の題名・要約・entity・本文埋込・配信時刻・媒体だけ。
本文の文ごとの埋込は使わない (配信時点で計算していない)。

先行の選び方 (``select_prior``): 過去 72 時間の alert のうち、本文 cos >= 0.70 かつ
事象群化 ML (pair_model、LLM 無し側の閾値) が「同じ出来事」と言ったもので最も新しいもの。
一致が無ければ None (記録しない)。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from src.cti.alert_dedup_gate import (
    ALERT_DEDUP_LOOKBACK_HOURS,
    EMBEDDING_COS_THRESHOLD,
    NEW_FACT_ENTITY_TYPES,
)
from src.cti.source_basis import SourceTier, classify_source_tier, tier_rank
from src.cti.state_lexicon import STATE_LEXICON, numbers_of, state_hits
from src.eventnews.pair_features import PairSide, pair_features
from src.eventnews.pair_model import PairModel

_ENTITY_TYPES: tuple[str, ...] = tuple(sorted(NEW_FACT_ENTITY_TYPES))
_STATE_KEYS: tuple[str, ...] = tuple(STATE_LEXICON)
_PRIMARY_TIERS: frozenset[str] = frozenset({"official", "research"})

#: 特徴の名前。**順序が意味を持つ** — 学習済みモデルの列順と一致させること。
FEATURE_NAMES: tuple[str, ...] = (
    "p_pair",
    "cos",
    "hours_apart",
    "same_feed",
    "n_ent_cur",
    "n_ent_prior",
    *(f"add_{t}" for t in _ENTITY_TYPES),
    *(f"miss_{t}" for t in _ENTITY_TYPES),
    "num_added",
    "num_removed",
    "unit_num_added",
    *(f"new_state_{k}" for k in _STATE_KEYS),
    "new_state_total",
    "tier_cur",
    "tier_prior",
    "tier_diff",
    "primary_added",
)


@dataclass(frozen=True)
class AlertSide:
    """特徴量を作るのに要る 1 記事分の材料。``time`` は今回なら配信時刻、先行なら投稿時刻。"""

    article_id: str
    title: str
    summary: str
    category: str
    feed_title: str
    feed_url: str
    account_class: str
    time: datetime
    entities: frozenset[tuple[str, str]]
    vector: np.ndarray


@dataclass(frozen=True)
class PriorChoice:
    """選ばれた先行 alert。"""

    side: AlertSide
    p_pair: float
    cos: float


def _pair_side(side: AlertSide) -> PairSide:
    return PairSide(
        article_id=side.article_id,
        title=side.title,
        category=side.category,
        feed_title=side.feed_title,
        published_at=side.time,
        entities=side.entities,
        vector=side.vector,
    )


def _cos(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if not na or not nb or a.shape != b.shape:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def pair_probability(cur: AlertSide, prior: AlertSide, pair_model: PairModel) -> float:
    """事象群化 ML の P(同じ出来事)。LLM 判定は呼ばない (alert_dedup_gate と同じ convention)。"""
    return pair_model.probability(pair_features(_pair_side(cur), _pair_side(prior)) + [0.0, 0.0])


def select_prior(
    cur: AlertSide,
    candidates: Sequence[AlertSide],
    pair_model: PairModel,
    *,
    lookback_hours: int = ALERT_DEDUP_LOOKBACK_HOURS,
    cos_threshold: float = EMBEDDING_COS_THRESHOLD,
) -> PriorChoice | None:
    """候補の alert のうち、cos と pair_model が一致する最も新しいものを返す (無ければ None)。"""
    earliest = cur.time - timedelta(hours=lookback_hours)
    best: PriorChoice | None = None
    for cand in candidates:
        if cand.article_id == cur.article_id or not (earliest <= cand.time < cur.time):
            continue
        cos = _cos(cur.vector, cand.vector)
        if cos < cos_threshold:
            continue
        features = pair_features(_pair_side(cur), _pair_side(cand)) + [0.0, 0.0]
        if not pair_model.joins(features, llm_available=False):
            continue
        if best is None or cand.time > best.side.time:
            best = PriorChoice(cand, pair_model.probability(features), cos)
    return best


def _entity_values(
    entities: frozenset[tuple[str, str]],
) -> set[tuple[str, str]]:
    return {(t, v.casefold()) for t, v in entities if t in NEW_FACT_ENTITY_TYPES}


def _tier(side: AlertSide) -> SourceTier:
    return classify_source_tier(side.feed_title, side.feed_url, account_class=side.account_class)


def _text(side: AlertSide) -> str:
    return f"{side.title}\n{side.summary}"


def alert_dedup_features(cur: AlertSide, prior: AlertSide, p_pair: float) -> list[float]:
    """今回 × 先行の特徴量。``FEATURE_NAMES`` と同じ順。欠損 (空・entity 無し) は 0 に落ちる。"""
    cur_ent, prior_ent = _entity_values(cur.entities), _entity_values(prior.entities)
    added, missing = cur_ent - prior_ent, prior_ent - cur_ent
    cur_plain, cur_unit = numbers_of(_text(cur))
    prior_plain, prior_unit = numbers_of(_text(prior))
    cur_state, prior_state = state_hits(_text(cur)), state_hits(_text(prior))
    new_state = cur_state - prior_state
    tier_cur, tier_prior = _tier(cur), _tier(prior)
    rank_cur, rank_prior = tier_rank(tier_cur), tier_rank(tier_prior)
    hours = abs((cur.time - prior.time).total_seconds()) / 3600.0
    return [
        float(p_pair),
        _cos(cur.vector, prior.vector),
        hours,
        float(cur.feed_title == prior.feed_title),
        float(len(cur_ent)),
        float(len(prior_ent)),
        *(float(sum(1 for t, _ in added if t == et)) for et in _ENTITY_TYPES),
        *(float(sum(1 for t, _ in missing if t == et)) for et in _ENTITY_TYPES),
        float(len(cur_plain - prior_plain)),
        float(len(prior_plain - cur_plain)),
        float(len(cur_unit - prior_unit)),
        *(float(k in new_state) for k in _STATE_KEYS),
        float(len(new_state)),
        float(rank_cur),
        float(rank_prior),
        float(rank_cur - rank_prior),
        float(tier_cur in _PRIMARY_TIERS and rank_cur > rank_prior),
    ]


__all__ = [
    "FEATURE_NAMES",
    "AlertSide",
    "PriorChoice",
    "alert_dedup_features",
    "pair_probability",
    "select_prior",
]
