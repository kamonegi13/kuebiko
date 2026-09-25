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
from src.cti.severity_axes import AXIS_FEATURE_NAMES, axis_feature_vector
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
    "pir",
)
#: 関与国 (involved_country) のうち任務上の主敵 + 日本。entity_counts に ``country:<ISO>`` で渡す
#: (2026-09-17 バックテストで国家系 (イラン / 北朝鮮) の事象を ML が落としていた対策)
NATION_FLAGS: tuple[str, ...] = ("CN", "RU", "KP", "IR", "JP")
#: **アクターの国家帰属** (辞書 actor_aliases.yaml の nation)。
#: entity_counts に ``actor_nation:<iso>`` で渡す。
#: 2026-09-19: LLM だけが開設した 14 件は bluenoroff / famous_chollima / mustang_panda など
#: **名前のついた国家系アクター**を見ていた。既存の n_actor (個数) では「どの国のアクターか」が
#: 写らず ML は 0.73 前後に置いていた。LLM の視点を決定論で特徴量へ移す (SYNTHESIS §52)
ACTOR_NATIONS: tuple[str, ...] = ("cn", "ru", "kp", "ir")
#: 辞書に載っている既知アクターが 1 件でも居るか (無名の犯罪グループと区別する)
KNOWN_ACTOR_KEY = "actor_known"
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
    #: 深刻度の軸 (欄名 → 選択肢)。未分類は None (one-hot が全 0)。2026-09-25
    axes: Mapping[str, str] | None = None


def _count_hits(text: str, patterns: tuple[re.Pattern[str], ...]) -> float:
    return float(sum(1 for p in patterns if p.search(text)))


def _one_hot(value: str, vocab: tuple[str, ...]) -> list[float]:
    return [1.0 if value == v else 0.0 for v in vocab]


#: 軸を足す前の列 (2026-09-25 までの detect の契約)。**深掘り選定 (deep_dive_features) が再利用**
#: しており、その同梱モデルはこの列で学習済み — 軸を足した FEATURE_NAMES を渡すと列ずれで
#: 深掘り ML が黙って外れる。深掘りを軸つきで作り直すまではこちらを使わせる。
BASE_FEATURE_NAMES: tuple[str, ...] = (
    ("importance",)
    + tuple(f"kind={k}" for k in KINDS)
    + tuple(f"category={c}" for c in CATEGORIES)
    + tuple(f"tier={t}" for t in TIERS)
    + tuple(f"n_{e}" for e in ENTITY_TYPES)
    + tuple(f"country={c}" for c in NATION_FLAGS)
    + tuple(f"actor_nation={n}" for n in ACTOR_NATIONS)
    + ("actor_known",)
    + (
        "n_entities",
        "japan_targeted",
        "followup_hits",
        "in_progress_hits",
        "closed_hits",
        "title_len",
    )
)
#: detect の列 = 基本 + 深刻度の軸 (2026-09-25、src/cti/severity_axes.py)。日本の小さな事案と
#: 追うべき事案を分ける — 評価 731 件で精度 54→62%・回収 65→75%・日本の小事案の誤開設 45→27
FEATURE_NAMES: tuple[str, ...] = BASE_FEATURE_NAMES + AXIS_FEATURE_NAMES


def feature_vector(a: DetectArticle) -> list[float]:
    """記事 1 件 → 特徴量ベクトル。順序は FEATURE_NAMES と 1:1 (テストが固定する)。"""
    vec = base_feature_vector(a) + axis_feature_vector(a.axes, f"{a.title}\n{a.summary}")
    if len(vec) != len(FEATURE_NAMES):  # pragma: no cover — 構造の不変条件
        raise RuntimeError(f"feature length {len(vec)} != names {len(FEATURE_NAMES)}")
    return vec


def base_feature_vector(a: DetectArticle) -> list[float]:
    """軸を除いた列 (``BASE_FEATURE_NAMES`` と 1:1)。深掘り選定が再利用する。"""
    text = f"{a.title}\n{a.summary}"
    counts = [float(a.entity_counts.get(e, 0)) for e in ENTITY_TYPES]
    nations = [1.0 if a.entity_counts.get(f"country:{c}", 0) else 0.0 for c in NATION_FLAGS]
    actor_nations = [
        1.0 if a.entity_counts.get(f"actor_nation:{n}", 0) else 0.0 for n in ACTOR_NATIONS
    ]
    known_actor = [1.0 if a.entity_counts.get(KNOWN_ACTOR_KEY, 0) else 0.0]
    vec = (
        [IMPORTANCE_ORD.get(a.importance, 0.0)]
        + _one_hot(a.kind if a.kind in KINDS else "other", KINDS)
        + _one_hot(a.category, CATEGORIES)
        + _one_hot(a.tier, TIERS)
        + counts
        + nations
        + actor_nations
        + known_actor
        + [
            float(
                sum(v for k, v in a.entity_counts.items() if ":" not in k and k != KNOWN_ACTOR_KEY)
            ),
            1.0 if is_japan_targeted_row(a.victim_country_iso, a.posted_channel) else 0.0,
            _count_hits(text, FOLLOWUP_PATTERNS),
            _count_hits(text, IN_PROGRESS_PATTERNS),
            _count_hits(text, CLOSED_PATTERNS),
            float(len(a.title)),
        ]
    )
    if len(vec) != len(BASE_FEATURE_NAMES):  # pragma: no cover — 構造の不変条件
        raise RuntimeError(f"feature length {len(vec)} != names {len(BASE_FEATURE_NAMES)}")
    return vec
