"""深掘りの時事性の「関連事案」判定を、dedup_key でなく事象の根で行う (2026-10-22)。

dedup_key は 1 群 1.01 記事 (§64: 実質的に何も束ねない最も粗い群化) だった。
同じ事象 (統合済みを含む根) に属する候補が 2 件以上あるときに「関連事案」とする。
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.digest.db_filter import DigestCandidate
from src.digest.deep_dive_selector import _timeliness_for

_END = datetime(2026, 10, 22, tzinfo=UTC)
_FRESH = "2026-10-21T00:00:00+00:00"


def _cand(article_id: str, dedup_key: str) -> DigestCandidate:
    return DigestCandidate(
        article_id=article_id,
        title="t",
        url=f"https://x.example/{article_id}",
        feed_title="F",
        importance="high",
        category="apt",
        posted_channel="brief",
        created_at=_FRESH,
        summary="s",
        dedup_key=dedup_key,
    )


class TestTimelinessCohort:
    def test_same_event_root_makes_candidates_related_despite_distinct_dedup_keys(self) -> None:
        cands = [_cand("a", "k1"), _cand("b", "k2")]

        got = _timeliness_for(
            cands,
            window_end=_END,
            actors=None,
            briefed=None,
            event_roots={"a": "ev-1", "b": "ev-1"},
        )

        assert got == {"a": 4.0, "b": 4.0}

    def test_same_dedup_key_alone_no_longer_relates_when_roots_say_different(self) -> None:
        cands = [_cand("a", "k1"), _cand("b", "k1")]

        got = _timeliness_for(
            cands,
            window_end=_END,
            actors=None,
            briefed=None,
            event_roots={"a": "ev-1", "b": "ev-2"},
        )

        assert got == {"a": 3.0, "b": 3.0}

    def test_candidates_outside_any_event_fall_back_to_dedup_key(self) -> None:
        cands = [_cand("a", "k1"), _cand("b", "k1")]

        got = _timeliness_for(cands, window_end=_END, actors=None, briefed=None, event_roots={})

        assert got == {"a": 4.0, "b": 4.0}

    def test_without_roots_behaviour_is_unchanged(self) -> None:
        cands = [_cand("a", "k1"), _cand("b", "k1"), _cand("c", "k9")]

        got = _timeliness_for(cands, window_end=_END, actors=None, briefed=None)

        assert got == {"a": 4.0, "b": 4.0, "c": 3.0}
