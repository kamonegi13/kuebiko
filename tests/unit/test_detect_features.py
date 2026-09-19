"""detect ML の特徴量 — 列の対応と語の照合を固定する (2026-09-17)。"""

from __future__ import annotations

from src.synthesis.grounded.detect_features import (
    FEATURE_NAMES,
    DetectArticle,
    feature_vector,
)


def _a(**kw: object) -> DetectArticle:
    base: dict[str, object] = {
        "article_id": "x",
        "title": "t",
        "summary": "s",
        "importance": "medium",
        "category": "breach",
        "tier": "news",
        "kind": "breach",
    }
    return DetectArticle(**{**base, **kw})  # type: ignore[arg-type]  # テスト用の緩い組立


def test_vector_length_matches_feature_names() -> None:
    assert len(feature_vector(_a())) == len(FEATURE_NAMES)


def test_kind_one_hot_and_unknown_kind_falls_back_to_other() -> None:
    v = dict(zip(FEATURE_NAMES, feature_vector(_a(kind="exploitation")), strict=True))
    assert v["kind=exploitation"] == 1.0 and v["kind=breach"] == 0.0
    v2 = dict(zip(FEATURE_NAMES, feature_vector(_a(kind="???")), strict=True))
    assert v2["kind=other"] == 1.0


def test_followup_and_in_progress_markers_are_counted() -> None:
    a = _a(title="E 社、不正アクセスの第2報を公開", summary="被害範囲は調査中")
    v = dict(zip(FEATURE_NAMES, feature_vector(a), strict=True))
    assert v["followup_hits"] >= 1.0
    assert v["in_progress_hits"] >= 2.0  # 調査中 + 被害範囲
    assert v["closed_hits"] == 0.0


def test_closed_markers_flag_one_shot_events() -> None:
    a = _a(title="米国財務省、イランのハッカー集団に経済制裁を発動", summary="")
    v = dict(zip(FEATURE_NAMES, feature_vector(a), strict=True))
    assert v["closed_hits"] == 1.0 and v["followup_hits"] == 0.0


def test_japan_and_entity_counts() -> None:
    a = _a(
        victim_country_iso="jp",
        entity_counts={"victim_org": 2, "cve": 1, "unknown": 5, "pir": 3, "country:KP": 1},
    )
    v = dict(zip(FEATURE_NAMES, feature_vector(a), strict=True))
    assert v["japan_targeted"] == 1.0
    assert v["n_victim_org"] == 2.0 and v["n_cve"] == 1.0 and v["n_pir"] == 3.0
    assert v["country=KP"] == 1.0 and v["country=CN"] == 0.0
    assert v["n_entities"] == 11.0  # 語彙外の型も総数には数える (country:* の補助 key は除く)
    assert v["importance"] == 1.0


def test_actor_nation_flags_and_known_actor() -> None:
    a = _a(entity_counts={"actor": 2, "actor_nation:kp": 2, "actor_known": 2, "cve": 1})
    v = dict(zip(FEATURE_NAMES, feature_vector(a), strict=True))
    assert v["actor_nation=kp"] == 1.0 and v["actor_nation=cn"] == 0.0
    assert v["actor_known"] == 1.0
    assert v["n_actor"] == 2.0
    # 補助 key (actor_nation:* / actor_known) は entity 総数に数えない
    assert v["n_entities"] == 3.0
