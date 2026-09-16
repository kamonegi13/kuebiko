"""detect (新規追跡の開設判定) の ML 化に使う記事特徴量 — 純粋関数 (2026-09-17)。

設計の根拠 (docs/research/llm_training/SYNTHESIS.md §47):
- 審判ラベル (importance / trackable / open / watch) に対し、**事象種別 (event_kind)** が
  追跡価値の主な担い手 (advisory → 見張り、breach / exploitation → 開設)。
- 09-10 の「detect ML 棄却」は現行由来ラベルの誤結論で、同じメタ特徴量でも AUC 0.82 出る。
- 埋込は効かない (書式を測る) ので入れない。

特徴量の語彙 (category / tier / kind / entity_type) はここが SSoT。学習時と本番で同じ
``feature_vector`` を通すこと (pair_model と同じ「列ずれ検証」を FEATURE_NAMES で担保)。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from src.cti.japan_relevance import is_japan_targeted_row
from src.eventnews.event_kind import KINDS

CATEGORIES: tuple[str, ...] = (
    "advisory",
    "apt",
    "apt_leak",
    "breach",
    "geopolitical",
    "incident",
    "malware",
    "opinion",
    "other",
    "policy",
    "recap",
    "research",
    "vulnerability",
)
TIERS: tuple[str, ...] = ("news", "official", "research", "social", "state_media")
ENTITY_TYPES: tuple[str, ...] = (
    "actor",
    "actor_provisional",
    "cve",
    "malware_family",
    "victim_org",
    "affected_product",
    "involved_country",
    "ttp",
)
IMPORTANCE_ORD: Mapping[str, float] = {"low": 0.0, "medium": 1.0, "high": 2.0}

#: 続報・進行中を示す語 (タイトル + 要約に対して照合)。追跡価値 = 「続報で見立てが動くか」の代理
FOLLOWUP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"第\s*[0-9０-９一二三四五六七八九十]+\s*報"),
    re.compile(r"続報|追加情報|追加調査|更新情報|続き"),
    re.compile(r"follow[- ]?up|update[sd]?\b", re.IGNORECASE),
)
IN_PROGRESS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"調査中|確認中|対応中|進行中|継続中|精査中|拡大中"),
    re.compile(r"調査結果|被害範囲|影響範囲|流出の可能性|漏えいの可能性|漏洩の可能性"),
    re.compile(r"ongoing|under investigation|actively exploited|in the wild", re.IGNORECASE),
)
CLOSED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"制裁|摘発|解体|逮捕|起訴|判決|年次報告|統計|レポート公開|まとめ|総括"),
    re.compile(
        r"sanction|indict|arrest|takedown|dismantle|annual report|statistics", re.IGNORECASE
    ),
)


@dataclass(frozen=True)
class DetectArticle:
    """特徴量の入力 (DB 行 + 事象種別 + entity 件数)。"""

    article_id: str
    title: str
    summary: str
    importance: str
    category: str
    tier: str
    kind: str
    victim_country_iso: str | None = None
    posted_channel: str | None = None
    entity_counts: Mapping[str, int] = field(default_factory=dict)


def _count_hits(text: str, patterns: tuple[re.Pattern[str], ...]) -> float:
    return float(sum(1 for p in patterns if p.search(text)))


def _one_hot(value: str, vocab: tuple[str, ...]) -> list[float]:
    return [1.0 if value == v else 0.0 for v in vocab]


FEATURE_NAMES: tuple[str, ...] = (
    ("importance",)
    + tuple(f"kind={k}" for k in KINDS)
    + tuple(f"category={c}" for c in CATEGORIES)
    + tuple(f"tier={t}" for t in TIERS)
    + tuple(f"n_{e}" for e in ENTITY_TYPES)
    + (
        "n_entities",
        "japan_targeted",
        "followup_hits",
        "in_progress_hits",
        "closed_hits",
        "title_len",
    )
)


def feature_vector(a: DetectArticle) -> list[float]:
    """記事 1 件 → 特徴量ベクトル。順序は FEATURE_NAMES と 1:1 (テストが固定する)。"""
    text = f"{a.title}\n{a.summary}"
    counts = [float(a.entity_counts.get(e, 0)) for e in ENTITY_TYPES]
    vec = (
        [IMPORTANCE_ORD.get(a.importance, 0.0)]
        + _one_hot(a.kind if a.kind in KINDS else "other", KINDS)
        + _one_hot(a.category, CATEGORIES)
        + _one_hot(a.tier, TIERS)
        + counts
        + [
            float(sum(a.entity_counts.values())),
            1.0 if is_japan_targeted_row(a.victim_country_iso, a.posted_channel) else 0.0,
            _count_hits(text, FOLLOWUP_PATTERNS),
            _count_hits(text, IN_PROGRESS_PATTERNS),
            _count_hits(text, CLOSED_PATTERNS),
            float(len(a.title)),
        ]
    )
    if len(vec) != len(FEATURE_NAMES):  # pragma: no cover — 構造の不変条件
        raise RuntimeError(f"feature length {len(vec)} != names {len(FEATURE_NAMES)}")
    return vec
