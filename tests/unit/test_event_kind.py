"""記事種別 (event_kind) — 種別対の特徴と退避の不変条件。"""

from __future__ import annotations

import numpy as np

from src.eventnews.event_kind import KIND_FEATURE_NAMES, kind_pair_features
from src.eventnews.pair_features import FEATURE_NAMES, PairSide, pair_features


def test_kind_features_match_the_declared_names() -> None:
    assert len(kind_pair_features("advisory", "exploitation")) == len(KIND_FEATURE_NAMES)
    assert FEATURE_NAMES[-len(KIND_FEATURE_NAMES) :] == KIND_FEATURE_NAMES


def test_advisory_vs_incident_pair_is_flagged() -> None:
    """「修正した」と「悪用した」は同じ CVE でも別の出来事 (利用者裁定の特徴化)。"""
    got = dict(zip(KIND_FEATURE_NAMES, kind_pair_features("advisory", "exploitation"), strict=True))
    assert got["kind_advisory_vs_incident"] == 1.0
    assert got["kind_same"] == 0.0


def test_same_kind_pair_is_not_flagged() -> None:
    got = dict(zip(KIND_FEATURE_NAMES, kind_pair_features("breach", "breach"), strict=True))
    assert got["kind_same"] == 1.0
    assert got["kind_advisory_vs_incident"] == 0.0
    assert got["kind_roundup_one"] == 0.0


def test_pair_features_defaults_to_other_kind() -> None:
    """kind 未指定 (未分類) は学習時の退避先 "other" と同じ挙動になる。"""
    from datetime import UTC, datetime

    side = PairSide(
        article_id="a",
        title="t",
        category="",
        feed_title="f",
        published_at=datetime(2026, 9, 3, tzinfo=UTC),
        entities=frozenset(),
        vector=np.asarray([1.0, 0.0], dtype=np.float32),
    )
    got = dict(zip(FEATURE_NAMES, pair_features(side, side), strict=True))
    assert got["kind_same"] == 1.0  # other == other
    assert got["kind_advisory_vs_incident"] == 0.0
