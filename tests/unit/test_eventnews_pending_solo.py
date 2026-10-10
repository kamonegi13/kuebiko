"""毎時の再生成の対象選び: 直近の high の単独報を拾う (2026-09 以降の取りこぼしの回帰)。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.eventnews import runner
from src.eventnews.models import ItemState
from src.ui.services import eventnews_hourly_job as job
from tests.unit.test_eventnews_solo_prompt import _member


def _state(*, importance: str, members: int, age_days: float) -> ItemState:
    at = datetime.now(UTC) - timedelta(days=age_days)
    return ItemState(
        item_id="i1",
        first_reported_at=at,
        last_reported_at=at,
        status="new",
        importance=importance,
        current_version=0,
        member_ids=tuple(f"a{n}" for n in range(members)),
    )


def test_multi_member_item_is_always_candidate() -> None:
    assert job._is_pending_candidate(_state(importance="low", members=2, age_days=90))


def test_recent_high_solo_is_candidate() -> None:
    assert job._is_pending_candidate(_state(importance="high", members=1, age_days=1))


def test_old_high_solo_is_not_candidate() -> None:
    assert not job._is_pending_candidate(_state(importance="high", members=1, age_days=30))


def test_medium_solo_is_not_candidate() -> None:
    assert not job._is_pending_candidate(_state(importance="medium", members=1, age_days=1))


def test_solo_needs_long_body() -> None:
    state = _state(importance="high", members=1, age_days=1)
    long_body = [_member(1, body="あ" * runner._SOLO_MIN_BODY_CHARS)]
    short_body = [_member(1, body="あ" * (runner._SOLO_MIN_BODY_CHARS - 1))]
    assert job._should_generate_solo(state, long_body)
    assert not job._should_generate_solo(state, short_body)
