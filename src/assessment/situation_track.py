"""台帳の型の判定 (2026-09-27) — アクターを追う台帳と、キャンペーンを追う台帳を分ける。

設計: docs/research/event_knowledge_graph.md §18 (STIX 2.1 / ATT&CK に合わせる)。

- ``actor``: STIX Intrusion Set の追跡。主題アクターが軸で期間の終わりが無い → 自動では閉じない
- ``campaign``: STIX Campaign。期間と標的で区切られた活動 (帰属は任意) → 休眠 → 終了 (従来どおり)

判定: 開設時の鍵に ``actor:<id>`` があり、そのアクターが **信頼できる経路で主題** になっている
開設記事があれば ``actor``。それ以外は ``campaign``。
信頼できる経路 = フィードの断言・見出しの別名・LLM high (LLM medium は精度 61% なので数えない、
§20)。アクターは辞書の group (実際の活動の主体) に限る — 機関 (organization / contractor) を
軸にした台帳は国家の動向で、持続的な侵入の主体ではない。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

TRACK_ACTOR = "actor"
TRACK_CAMPAIGN = "campaign"

#: 主題として信頼する経路 (subject_actor_source)
_TRUSTED_SOURCES = frozenset({"feed", "feed_match", "title"})
_ACTOR_KEY_PREFIX = "actor:"


def _trusted(source: str, confidence: str) -> bool:
    return source in _TRUSTED_SOURCES or (source == "llm" and confidence == "high")


def decide_track(
    anchors: Iterable[str],
    seed_subjects: Iterable[tuple[str, str, str]],
    *,
    is_group: Callable[[str], bool],
) -> str:
    """台帳の型 (``actor`` / ``campaign``)。

    Args:
        anchors: 台帳の鍵 ("type:value")
        seed_subjects: 開設記事ごとの (主題 id のカンマ区切り, source, confidence)
        is_group: アクター id が辞書の group か
    """
    anchor_actors = {
        a[len(_ACTOR_KEY_PREFIX) :].strip().lower()
        for a in anchors
        if a.lower().startswith(_ACTOR_KEY_PREFIX)
    }
    if not anchor_actors:
        return TRACK_CAMPAIGN
    for ids_csv, source, confidence in seed_subjects:
        if not _trusted(source, confidence):
            continue
        subjects = {s.strip().lower() for s in ids_csv.split(",") if s.strip()}
        if any(is_group(a) for a in subjects & anchor_actors):
            return TRACK_ACTOR
    return TRACK_CAMPAIGN


def decide_track_for(
    anchors: Iterable[str],
    article_ids: Iterable[str],
    subjects_by_article: Mapping[str, tuple[str, str, str]],
    *,
    is_group: Callable[[str], bool],
) -> str:
    """開設記事の id から型を決める (記事の主題は ``subjects_by_article`` から引く)。"""
    seeds = [subjects_by_article[a] for a in article_ids if a in subjects_by_article]
    return decide_track(anchors, seeds, is_group=is_group)


def registry_is_group() -> Callable[[str], bool]:
    """アクター辞書で group かを判定する関数 (辞書に無い id は False)。"""
    from src.cti.actor_normalizer import load_actor_aliases

    registry = load_actor_aliases()

    def is_group(actor_id: str) -> bool:
        actor = registry.by_id(actor_id)
        return actor is not None and actor.kind == "group"

    return is_group
