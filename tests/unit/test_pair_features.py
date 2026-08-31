"""記事ペアの特徴量の不変条件。

⚠ **列順は学習済みモデルと対応している。**順序を変えるとモデルが別の意味の数値を
読むので、名前と長さの一致をここで固定する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np

from src.eventnews.pair_features import (
    FEATURE_NAMES,
    PairSide,
    concrete_shared_names,
    pair_features,
)


def _side(
    title: str = "見出し",
    *,
    entities: set[tuple[str, str]] | None = None,
    vector: list[float] | None = None,
    summary_vector: list[float] | None = None,
    category: str = "breach",
    feed: str = "媒体A",
    hours: float = 0.0,
) -> PairSide:
    return PairSide(
        article_id=f"a-{title}",
        title=title,
        category=category,
        feed_title=feed,
        published_at=datetime(2026, 8, 31, tzinfo=UTC) + timedelta(hours=hours),
        entities=frozenset(entities or set()),
        vector=np.asarray(vector or [1.0, 0.0, 0.0], dtype=np.float32),
        summary_vector=(
            np.asarray(summary_vector, dtype=np.float32) if summary_vector is not None else None
        ),
    )


def test_feature_vector_matches_the_declared_names() -> None:
    """列数と名前が一致する (モデルの列順の対応が崩れない)。"""
    got = pair_features(_side(), _side())
    assert len(got) == len(FEATURE_NAMES)


def test_summary_cosine_falls_back_to_body_when_absent() -> None:
    """要約が無い記事 (実測で 11%) でも欠測にしない。差分は 0 になる。"""
    got = dict(zip(FEATURE_NAMES, pair_features(_side(), _side()), strict=True))
    assert got["cos_summary"] == got["cos"]
    assert got["cos_delta"] == 0.0


def test_summary_cosine_is_used_when_present() -> None:
    a = _side(vector=[1.0, 0.0], summary_vector=[1.0, 0.0])
    b = _side(vector=[0.0, 1.0], summary_vector=[1.0, 0.0])
    got = dict(zip(FEATURE_NAMES, pair_features(a, b), strict=True))
    assert got["cos"] < 0.01
    assert got["cos_summary"] > 0.99, "要約が揃っていれば近いと出るべき"


def test_same_name_in_two_types_counts_once() -> None:
    """``kimsuky`` は actor と malware_family の両方に出る (実例)。"""
    ents = {("actor", "kimsuky"), ("malware_family", "Kimsuky")}
    got = dict(
        zip(FEATURE_NAMES, pair_features(_side(entities=ents), _side(entities=ents)), strict=True)
    )
    assert got["shared_names"] == 1.0


def test_victim_conflict_is_flagged() -> None:
    a = _side(entities={("actor", "play"), ("victim_org", "alpha")})
    b = _side(entities={("actor", "play"), ("victim_org", "beta")})
    got = dict(zip(FEATURE_NAMES, pair_features(a, b), strict=True))
    assert got["victim_conflict"] == 1.0


def test_roundup_and_count_markers() -> None:
    a = _side("Qilin ランサムウェア、新たに 5 つの組織を被害者として公開")
    b = _side("Qilin: BLISS 1041 (MT)")
    got = dict(zip(FEATURE_NAMES, pair_features(a, b), strict=True))
    assert got["roundup_one"] == 1.0
    assert got["roundup_both"] == 0.0
    assert got["count_one"] == 1.0


def test_hours_apart_is_capped() -> None:
    got = dict(zip(FEATURE_NAMES, pair_features(_side(), _side(hours=10_000)), strict=True))
    assert got["hours_apart"] == 336.0


def test_concrete_names_exclude_actor_names() -> None:
    """アクターは「誰が」であって「何が起きたか」ではない。"""
    ents = {("actor", "kimsuky"), ("malware_family", "Kimsuky")}
    assert concrete_shared_names(_side(entities=ents), _side(entities=ents)) == set()

    ents2 = {("actor", "kimsuky"), ("tool", "Chrome Remote Desktop")}
    assert concrete_shared_names(_side(entities=ents2), _side(entities=ents2)) == {
        "chrome remote desktop"
    }
