"""ML 前段の replay — 候補セットの合成と採点 (2026-09-18)。"""

from __future__ import annotations

from typing import Any

from scripts.replay_detect_prefilter import narrow, score_arm
from src.synthesis.grounded.detect_features import DetectArticle
from src.synthesis.grounded.incremental import DetectedClaim, DetectResult


def _art(aid: str, **kw: Any) -> DetectArticle:
    base: dict[str, Any] = {
        "article_id": aid,
        "title": "t",
        "summary": "s",
        "importance": "medium",
        "category": "breach",
        "tier": "news",
        "kind": "breach",
    }
    return DetectArticle(**{**base, **kw})


def test_narrow_keeps_top_k_plus_floor_and_drops_rollups() -> None:
    pool = [{"article_id": a, "title": "t"} for a in ("a", "b", "roll", "jp")]
    arts = {
        "a": _art("a"),
        "b": _art("b"),
        "roll": _art("roll", title="Microsoftが8月の月例更新を公開"),
        "jp": _art("jp", victim_country_iso="JP"),
    }
    scores = {"a": 0.9, "b": 0.5, "roll": 0.99, "jp": 0.1}

    got = [x["article_id"] for x in narrow(pool, scores, arts, top_k=1)]

    assert got == ["a", "jp"]  # 上位 1 + 日本標的 breach の下限保証、勧告は除外


def test_score_arm_counts_articles_by_judge_label() -> None:
    result = DetectResult(
        open=(
            DetectedClaim(claim="c1", domain="cyber", article_ids=("a", "b")),
            DetectedClaim(claim="c2", domain="cyber", article_ids=("c",)),
        ),
        rejected=(),
        overflow=0,
    )
    gold = {
        "a": {"gold_open": True, "watch": False},
        "b": {"gold_open": False, "watch": True},
        "c": {"gold_open": False, "watch": False},
    }

    assert score_arm(result, gold) == {
        "claims": 2,
        "articles": 3,
        "open": 1,
        "watch": 1,
        "drop": 1,
        "unjudged": 0,
    }
    assert score_arm(result, {})["unjudged"] == 3
