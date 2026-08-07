"""src/spotlight/models.py: schema round-trip テスト。"""

from __future__ import annotations

from datetime import UTC, datetime

from src.spotlight.models import KeyEvent, SpotlightRecord


def test_key_event_round_trip() -> None:
    ke = KeyEvent(
        article_id="a1",
        title="t",
        url="u",
        feed_title="f",
        importance="high",
        published_at="2026-05-24T00:00:00+00:00",
    )
    d = ke.model_dump()
    ke2 = KeyEvent.model_validate(d)
    assert ke2 == ke


def test_spotlight_record_defaults() -> None:
    now = datetime.now(UTC)
    r = SpotlightRecord(
        pir_id="pir_test",
        pir_title="Test PIR",
        period_type="weekly",
        period_start=now,
        period_end=now,
        headline="",
        outlook="",
    )
    assert r.key_events == []
    assert r.article_count == 0
    assert r.llm_model == ""
    assert r.generated_at is not None


def test_spotlight_with_key_events() -> None:
    now = datetime.now(UTC)
    events = [
        KeyEvent(article_id="a1", title="evt1"),
        KeyEvent(article_id="a2", title="evt2"),
    ]
    r = SpotlightRecord(
        pir_id="pir_china_apt",
        pir_title="China APT",
        period_type="weekly",
        period_start=now,
        period_end=now,
        headline="headline",
        outlook="outlook",
        key_events=events,
        article_count=5,
        llm_model="gemma4:26b",
    )
    assert len(r.key_events) == 2
    assert r.key_events[0].article_id == "a1"
    assert r.llm_model == "gemma4:26b"
