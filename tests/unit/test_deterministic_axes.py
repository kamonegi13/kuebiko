"""深掘り rubric の計算で決まる軸 (2026-09-21)。

教師 1,260 行の実測で timeliness は 74% が満点 5・σ0.60 と、重み 0.20 を持ちながら
選抜に寄与していなかった。日付との一致は 6.1%。**LLM に聞くべきでないものを
聞いていた**ので、計算で出す。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.digest.deterministic_axes import novelty_floor, parse_ts, timeliness_score

_END = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)


def _at(days_ago: float) -> str:
    from datetime import timedelta

    return (_END - timedelta(days=days_ago)).isoformat()


@pytest.mark.parametrize(
    ("days_ago", "expected"),
    [(0.5, 3.0), (7.0, 3.0), (8.0, 2.0), (21.0, 2.0), (30.0, 2.0), (31.0, 1.0), (400.0, 1.0)],
)
def test_age_alone_decides_when_no_actor_overlap(days_ago: float, expected: float) -> None:
    assert timeliness_score(created_at=_at(days_ago), window_end=_END) == expected


def test_actor_briefed_this_week_is_the_top_anchor() -> None:
    """anchor 5 =「今週速報した actor/campaign の深い背景」。古い記事でも該当する。"""
    got = timeliness_score(
        created_at=_at(40),  # 日数だけなら 1
        window_end=_END,
        article_actors=["lazarus"],
        briefed_actors=["lazarus", "apt29"],
    )

    assert got == 5.0


def test_same_cluster_appearing_twice_lifts_this_week_to_four() -> None:
    """anchor 4 =「同時期の関連事案」。同じ事案が候補内に複数あることで判定する。"""
    got = timeliness_score(
        created_at=_at(2), window_end=_END, dedup_key="k1", cohort_dedup_keys=["k1"]
    )

    assert got == 4.0


def test_cluster_repeat_does_not_lift_an_old_article() -> None:
    got = timeliness_score(
        created_at=_at(25), window_end=_END, dedup_key="k1", cohort_dedup_keys=["k1"]
    )

    assert got == 2.0


def test_an_article_without_an_actor_is_not_penalised_against_apt_articles() -> None:
    """⚠ 候補全体の actor 和集合を代用にした初版は、actor 名の付かない記事
    (脆弱性の実悪用・国内侵害) を構造的に 1 点下げ、VMware vCenter の実悪用や
    国内被害を選抜から押し出した (2026-09-21 実測)。同じ日に出た記事は、
    actor の有無で差が付いてはいけない。"""
    with_actor = timeliness_score(created_at=_at(2), window_end=_END, article_actors=["lazarus"])
    without = timeliness_score(created_at=_at(2), window_end=_END)

    assert with_actor == without == 3.0


def test_unparsable_timestamp_yields_none_not_a_guess() -> None:
    assert timeliness_score(created_at=None, window_end=_END) is None
    assert timeliness_score(created_at="いつか", window_end=_END) is None


class TestNoveltyFloor:
    """⭐ 確定する端点だけ返す。中間 (新 IoC か・重要な続報か) は中身の判断。"""

    def test_already_selected_cluster_is_zero(self) -> None:
        assert novelty_floor(dedup_key="k1", selected_keys=["k1", "k2"]) == 0.0

    def test_unseen_cluster_is_left_to_the_llm(self) -> None:
        assert novelty_floor(dedup_key="k9", selected_keys=["k1"]) is None

    def test_missing_key_is_left_to_the_llm(self) -> None:
        assert novelty_floor(dedup_key=None, selected_keys=["k1"]) is None


def test_parse_ts_handles_z_suffix() -> None:
    assert parse_ts("2026-09-21T00:00:00Z") == _END
    assert parse_ts("") is None
