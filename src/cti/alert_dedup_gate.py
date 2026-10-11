"""alert channel: 同じ事象への重複 alert を抑止する関門 (R1b、2026-10-11)。

利用者決定 (10-11): 同じ事象への重複の alert を止める。ただし新しい事実を足した続報
(``event_item_members.contributed_new_facts``) は alert のまま残す。
根拠: R1 の 30 日再現 (data/eval/r1_report.md) で alert 213 件中 52 件 (24%) が
同一 event group 内の重複、21 件が新事実の続報だった (data/eval/r1_duplicate_alerts_v1.json)。

## v1 (タイトル Jaccard) を作り直した経緯

当初実装 (dedup_key/advisory id 完全一致 + タイトル signature Jaccard>=0.85) を
``r1_duplicate_alerts_v1.json`` の実例 5 組に当てたところ **Jaccard は 0.10〜0.19 で
再現率ほぼ 0** だった。これらは「同一記事の再掲」ではなく「同じ事象を別媒体が別の
書き方で報じた」組 (例: 「中国系ハッカー、Chromeのゼロデイ...」vs「中国系アクター
UTA0565、Chrome/Windows...」) で、字面の一致はそもそも手がかりとして弱い。
オーケストレータ指示 (2026-10-11) により、以下へ作り直した。

## v2: embedding 候補検索 + 事象群化 ML (pair_model) で判定

1. **候補検索**: 過去 ``ALERT_DEDUP_LOOKBACK_HOURS`` (72h) 以内に **channel="alert" で
   投稿済み** の記事から、本文 embedding コサイン類似度 >= ``EMBEDDING_COS_THRESHOLD``
   (0.70) のものを候補にする (``RunHistoryRepository.find_similar_embeddings``)。
2. **同事象判定**: 候補ごとに、事象群化で使っている学習済みモデル
   (``src.eventnews.pair_model``、``config/models/pair_model.json``) へ同じ特徴量
   (``src.eventnews.pair_features.pair_features``) を渡して P(同じ出来事) を読む。
   LLM 判定 (``llm_same``/``llm_known``) は配信時点では呼ばない (コスト) ので
   ``llm_available=False`` の閾値 (``threshold_llm_off``、学習時実測 85%) を使う。
   ``summary_vector`` (題名+要約の埋込) は毎時の段でしか計算されない
   (``src.storage.repo_summary_embeddings``) ため常に ``None`` (本文 embedding で代替、
   ``pair_features`` 自身の既定動作)。``kind`` (event_kind 分類) も毎時の段専属のため
   既定値 ``"other"`` を使う (学習時の未分類退避先と同じ)。
3. dedup_key/advisory id の完全一致は、コスト 0 の前段フィルタとして残す
   (embedding/ML より先に判定、一致したら即採用)。

## 新事実の有無 (簡易版、決定的)

``contributed_new_facts`` (ML 判定、毎時の段でしか出ない) は配信時点では使えない。
オーケストレータ指示により、**帰属・手口・被害規模を表す固有名詞 (actor/malware_family/
tool/cve/victim_org) が先行記事の entity 集合に無い新しい語として加わっているか**を
決定的に比較する簡易版にする (``has_new_named_entities``)。先行記事の entity は
``article_entities`` テーブルの永続化済み値 (``RunHistoryRepository.get_article_entities``)、
当記事の entity は ``msg`` から ``build_current_entities`` で組み立てる
(malware_normalizer/NVD 逆引き等の正規化は省く簡易版 — 完全な
``src.pipeline.persistence._persist_article_entities`` の代替ではない)。

rollback: 環境変数 ``ALERT_DEDUP_EVENT`` を既定 ("0") のままにすると無効化。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from src.cti.content_dedup import extract_advisory_ids
from src.eventnews.pair_features import PairSide, pair_features
from src.eventnews.pair_model import load_model
from src.logging_config import get_logger
from src.storage.run_history import ArticleRecord, RunHistoryRepository

_log = get_logger(__name__)

GATE_ENV = "ALERT_DEDUP_EVENT"
ALERT_CHANNEL = "alert"

#: 利用者指示 (2026-10-11): 過去 72 時間の alert 投稿を候補にする。
ALERT_DEDUP_LOOKBACK_HOURS = 72
#: embedding コサイン類似度の候補化しきい値 (利用者指示の例値)。
EMBEDDING_COS_THRESHOLD = 0.70
#: 候補検索の上限件数 (alert channel に絞り込む前の取得数、流量に余裕を持たせる)。
_CANDIDATE_TOP_K = 30
#: 先行記事の exact-match 探索上限 (dedup_key/advisory id、コスト 0 の前段)。
_EXACT_MATCH_CANDIDATE_LIMIT = 1200

#: 「帰属・手口・被害規模」を表す entity type (pair_features の entities と同じ語彙)。
NEW_FACT_ENTITY_TYPES: frozenset[str] = frozenset(
    {"actor", "malware_family", "tool", "cve", "victim_org"}
)

_CVE_RE = re.compile(r"CVE-(?:19|20)\d{2}-\d{4,7}", re.IGNORECASE)


def is_alert_dedup_event_enabled() -> bool:
    """``ALERT_DEDUP_EVENT`` (既定 off)。"0" 以外を明示したときだけ有効化する。"""
    value = os.environ.get(GATE_ENV, "0").strip()
    return value not in ("", "0")


@dataclass(frozen=True)
class AlertDedupMatch:
    """一致した先行 alert 記事。"""

    prior_article_id: str
    prior_title: str
    reason: str  # "dedup_key" | "advisory_id" | "pair_model"
    prior_entities: frozenset[tuple[str, str]] = frozenset()
    probability: float = 0.0


def build_current_entities(
    *,
    iocs: list[str] | None,
    mitre_techniques: list[str] | None,
    malware_families: list[str] | None,
    tools: list[str] | None,
    detected_actor_ids: list[str] | None,
    victim_orgs: list[str] | None,
) -> frozenset[tuple[str, str]]:
    """``msg`` (未永続化) から軽量に entity 集合を組み立てる (簡易版)。

    ``src.pipeline.persistence._persist_article_entities`` の malware_normalizer 正規化・
    NVD 逆引きは省く (配信時点のゲート判定に使う粗い比較で十分という判断、
    オーケストレータ指示の「簡易版でよい」に対応)。
    """
    entities: set[tuple[str, str]] = set()
    for tech in mitre_techniques or []:
        t = str(tech).strip().upper()
        if _CVE_RE.fullmatch(t):
            entities.add(("cve", t))
    for ioc in iocs or []:
        m = _CVE_RE.search(str(ioc))
        if m:
            entities.add(("cve", m.group(0).upper()))
    for fam in malware_families or []:
        if str(fam).strip():
            entities.add(("malware_family", str(fam).strip()))
    for tool in tools or []:
        if str(tool).strip():
            entities.add(("tool", str(tool).strip()))
    for actor_id in detected_actor_ids or []:
        if str(actor_id).strip():
            entities.add(("actor", str(actor_id).strip()))
    for org in victim_orgs or []:
        if str(org).strip():
            entities.add(("victim_org", str(org).strip()))
    return frozenset(entities)


def has_new_named_entities(
    current: frozenset[tuple[str, str]], prior: frozenset[tuple[str, str]]
) -> bool:
    """帰属・手口・被害規模の固有名詞で、先行記事に無い新しい語が加わっているか。

    比較は大小文字を無視した値一致 (type, value.casefold())。``NEW_FACT_ENTITY_TYPES``
    以外の type (ioc_* 等) は比較対象にしない。
    """
    cur = {(t, v.casefold()) for t, v in current if t in NEW_FACT_ENTITY_TYPES}
    pri = {(t, v.casefold()) for t, v in prior if t in NEW_FACT_ENTITY_TYPES}
    return bool(cur - pri)


def _exact_match_candidates(
    repo: RunHistoryRepository, *, candidate_article_id: str | None
) -> list[ArticleRecord]:
    candidates = repo.list_recent_posted_articles(
        lookback_hours=ALERT_DEDUP_LOOKBACK_HOURS, limit=_EXACT_MATCH_CANDIDATE_LIMIT
    )
    return [
        c
        for c in candidates
        if c.posted_channel == ALERT_CHANNEL
        and (not candidate_article_id or c.article_id != candidate_article_id)
    ]


def find_prior_alert_duplicate(
    *,
    repo: RunHistoryRepository,
    dedup_key: str,
    title: str,
    summary: str = "",
    url: str = "",
    category: str = "",
    feed_title: str = "",
    published_at: datetime,
    vector: list[float] | None,
    embedding_model: str,
    entities: frozenset[tuple[str, str]],
    candidate_article_id: str | None = None,
    lookback_hours: int = ALERT_DEDUP_LOOKBACK_HOURS,
    cos_threshold: float = EMBEDDING_COS_THRESHOLD,
) -> AlertDedupMatch | None:
    """過去 channel="alert" 投稿の中から「同じ事象の重複」とみなせる記事を探す。

    watch/japan_watch 等への重複はこの gate の対象外 (一般 dedup が別途担当する)。
    1. dedup_key 完全一致 → advisory id 完全一致 (コスト 0)
    2. embedding コサイン候補 → 事象群化 ML (pair_model) で P(同じ出来事) を判定
    """
    alert_candidates = _exact_match_candidates(repo, candidate_article_id=candidate_article_id)
    if alert_candidates:
        dedup_key_norm = (dedup_key or "").strip()
        if dedup_key_norm:
            for art in alert_candidates:
                if (art.dedup_key or "").strip() == dedup_key_norm:
                    _log.info(
                        "alert_dedup_match", reason="dedup_key", prior_article_id=art.article_id
                    )
                    prior_entities = frozenset(repo.get_article_entities(art.article_id))
                    return AlertDedupMatch(
                        art.article_id, art.title or "", "dedup_key", prior_entities
                    )

        ids_new = extract_advisory_ids(url, title, summary)
        if ids_new:
            for art in alert_candidates:
                art_ids = extract_advisory_ids(art.url or "", art.title or "", art.summary or "")
                shared = ids_new & art_ids
                if shared:
                    _log.info(
                        "alert_dedup_match",
                        reason="advisory_id",
                        shared_ids=sorted(shared),
                        prior_article_id=art.article_id,
                    )
                    prior_entities = frozenset(repo.get_article_entities(art.article_id))
                    return AlertDedupMatch(
                        art.article_id, art.title or "", "advisory_id", prior_entities
                    )

    if not vector or not embedding_model:
        return None
    model = load_model()
    if model is None:
        return None

    sim_candidates = repo.find_similar_embeddings(
        vector,
        model=embedding_model,
        top_k=_CANDIDATE_TOP_K,
        threshold=cos_threshold,
        window_hours=lookback_hours,
    )
    if not sim_candidates:
        return None
    urls = [c[1] for c in sim_candidates]
    records = repo.get_articles_by_urls(urls)

    left = PairSide(
        article_id=candidate_article_id or "",
        title=title,
        category=category,
        feed_title=feed_title,
        published_at=published_at,
        entities=entities,
        vector=np.asarray(vector, dtype=np.float32),
    )

    best: AlertDedupMatch | None = None
    for url_hash_val, cand_url, _sim in sim_candidates:
        cand_art = records.get(cand_url)
        if (
            cand_art is None
            or cand_art.posted_channel != ALERT_CHANNEL
            or cand_art.status != "posted"
        ):
            continue
        if candidate_article_id and cand_art.article_id == candidate_article_id:
            continue
        prior_vector = repo.get_embedding(url_hash_val)
        if prior_vector is None:
            continue
        prior_entities = frozenset(repo.get_article_entities(cand_art.article_id))
        right = PairSide(
            article_id=cand_art.article_id,
            title=cand_art.title or "",
            category=cand_art.category or "",
            feed_title=cand_art.feed_title or "",
            published_at=cand_art.created_at,
            entities=prior_entities,
            vector=prior_vector,
        )
        # LLM 判定 (llm_same/llm_known) は配信時点では呼ばない (コスト) ので
        # 「不明」で埋め、llm_available=False 側の閾値で判定する
        # (src/ui/services/eventnews_merge_job.py の群化本体と同じ convention)。
        features = pair_features(left, right) + [0.0, 0.0]
        if not model.joins(features, llm_available=False):
            continue
        prob = model.probability(features)
        if best is None or prob > best.probability:
            best = AlertDedupMatch(
                cand_art.article_id, cand_art.title or "", "pair_model", prior_entities, prob
            )
    if best is not None:
        _log.info(
            "alert_dedup_match",
            reason="pair_model",
            probability=round(best.probability, 3),
            prior_article_id=best.prior_article_id,
        )
    return best


__all__ = [
    "ALERT_CHANNEL",
    "ALERT_DEDUP_LOOKBACK_HOURS",
    "EMBEDDING_COS_THRESHOLD",
    "NEW_FACT_ENTITY_TYPES",
    "AlertDedupMatch",
    "GATE_ENV",
    "build_current_entities",
    "find_prior_alert_duplicate",
    "has_new_named_entities",
    "is_alert_dedup_event_enabled",
]
