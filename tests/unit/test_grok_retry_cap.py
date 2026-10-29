"""Grok の描画されなかったレポートの取り直しの上限 (P0-25)。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.grok.fetcher import GrokFetchResult
from src.storage.run_history import RunHistoryRepository
from src.tools.grok_retry import (
    GROK_EXPIRY_WARN_LEAD_MINUTES,
    GrokRetryTracker,
    grok_summary_every,
)
from src.tools.imap_client import EmailMessage
from src.tools.source_router import GrokEmailSource

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
LOOKBACK = 1440


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "g.db")


def _email(uid: str = "1", received: datetime | None = None) -> EmailMessage:
    return EmailMessage(
        uid=uid,
        subject="Grok report",
        sender="noreply@x.ai",
        received_at=received or NOW - timedelta(hours=1),
        body_text="visit",
        body_html="<a>x</a>",
        extracted_urls=["https://grok.com/chat/x"],
    )


def _unrendered() -> GrokFetchResult:
    return GrokFetchResult(
        url="https://grok.com/chat/x",
        final_url="https://grok.com/c/x",
        html="<html><body>Grok</body></html>",
        body_text="",
        title="Grok",
        success=True,
    )


def _source(
    repo: RunHistoryRepository | None,
    emails: list[EmailMessage],
    *,
    every: int = 6,
    now: datetime = NOW,
    grok_result: GrokFetchResult | None = None,
) -> GrokEmailSource:
    imap = AsyncMock()
    imap.fetch_unread.return_value = emails
    grok = AsyncMock()
    grok.fetch.return_value = grok_result or _unrendered()
    tracker = GrokRetryTracker(
        repo, summary_every=every, lookback_minutes=LOOKBACK, clock=lambda: now
    )
    return GrokEmailSource(
        imap,
        grok,
        MagicMock(),
        mark_as_read=True,
        lookback_minutes=LOOKBACK,
        retry_tracker=tracker if repo is not None else None,
    )


async def test_first_failure_alerts_then_repeats_are_silent(
    repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.tools.source_router._trafilatura_extract", lambda h, **k: "")
    alerts: list[int] = []
    for _ in range(2):
        s = _source(repo, [_email()])
        await s.fetch(5)
        alerts.append(s.last_not_rendered_count)
    assert alerts == [1, 0]


async def test_never_stops_retrying_and_summarises_every_n(
    repo: RunHistoryRepository,
) -> None:
    summaries: list[int] = []
    alerts: list[int] = []
    for _ in range(7):
        s = _source(repo, [_email()], every=6)
        await s.fetch(5)
        assert cast_calls(s) == 1  # 毎回取りにいく
        summaries.append(s.last_unrendered_summary_count)
        alerts.append(s.last_not_rendered_count)
    # 最初の失敗で通知、6 回目で要約、それ以外は静か
    assert alerts == [1, 0, 0, 0, 0, 0, 0]
    assert summaries == [0, 0, 0, 0, 0, 1, 0]


async def test_every_zero_notifies_each_time(repo: RunHistoryRepository) -> None:
    for _ in range(3):
        s = _source(repo, [_email()], every=0)
        await s.fetch(5)
        assert s.last_not_rendered_count == 1


def cast_calls(s: GrokEmailSource) -> int:
    grok = s._grok
    assert isinstance(grok, AsyncMock)
    return int(grok.fetch.await_count)


async def test_success_resets_count(repo: RunHistoryRepository) -> None:
    for _ in range(2):
        await _source(repo, [_email()]).fetch(5)
    ok = GrokFetchResult(
        url="u",
        final_url="https://grok.com/c/x",
        html="<html/>",
        body_text="real report body",
        title="t",
        success=True,
    )
    s = _source(repo, [_email()], grok_result=ok)
    assert len(await s.fetch(5)) == 1
    assert repo.get_grok_attempt("1") is None


async def test_no_tracker_keeps_old_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.tools.source_router._trafilatura_extract", lambda h, **k: "")
    for _ in range(5):
        s = _source(None, [_email()])
        await s.fetch(5)
        assert s.last_not_rendered_count == 1


async def test_warns_once_before_leaving_lookback(repo: RunHistoryRepository) -> None:
    received = NOW - timedelta(minutes=LOOKBACK - GROK_EXPIRY_WARN_LEAD_MINUTES + 10)
    counts = []
    for _ in range(3):
        s = _source(repo, [_email(received=received)])
        await s.fetch(5)
        counts.append(s.last_expiring_count)
    assert counts == [1, 0, 0]  # 一度だけ警告する


async def test_no_warning_far_from_expiry(repo: RunHistoryRepository) -> None:
    await _source(repo, [_email()]).fetch(5)
    s = _source(repo, [_email()])
    await s.fetch(5)
    assert s.last_expiring_count == 0


async def test_store_failure_does_not_break_fetch(repo: RunHistoryRepository) -> None:
    s = _source(repo, [_email()])
    s._retry_tracker._store = MagicMock(  # type: ignore[union-attr]
        get_grok_attempt=MagicMock(side_effect=RuntimeError("db")),
        record_grok_failure=MagicMock(side_effect=RuntimeError("db")),
    )
    assert await s.fetch(5) == []
    assert s.last_not_rendered_count == 1  # 記録できなければ従来どおり警報側に倒す
    assert s._retry_tracker.store_errors >= 1  # type: ignore[union-attr]


def test_repo_roundtrip_and_purge(repo: RunHistoryRepository) -> None:
    a = repo.record_grok_failure("7", NOW, "report_not_rendered", when=NOW)
    b = repo.record_grok_failure("7", NOW, "report_not_rendered", when=NOW)
    assert (a.fail_count, b.fail_count) == (1, 2)
    repo.mark_grok_expiry_warned("7", when=NOW)
    got = repo.get_grok_attempt("7")
    assert got is not None and got.expiry_warned_at
    repo.record_grok_failure("old", NOW, "x", when=datetime.now(UTC) - timedelta(days=60))
    assert repo.purge_grok_fetch_attempts(days=30) == 1
    repo.clear_grok_attempt("7")
    assert repo.get_grok_attempt("7") is None


def test_cap_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROK_UNRENDERED_SUMMARY_EVERY", raising=False)
    assert grok_summary_every() == 6
    monkeypatch.setenv("GROK_UNRENDERED_SUMMARY_EVERY", "0")
    assert grok_summary_every() == 0
    monkeypatch.setenv("GROK_UNRENDERED_SUMMARY_EVERY", "abc")
    assert grok_summary_every() == 6


async def test_dry_run_style_untracked_source_writes_nothing(repo: RunHistoryRepository) -> None:
    """dispatch は dry-run で store を渡さない (tracker は無効) — 記録は一切増えない。"""
    imap = AsyncMock()
    imap.fetch_unread.return_value = [_email()]
    grok = AsyncMock()
    grok.fetch.return_value = _unrendered()
    tracker = GrokRetryTracker(None, summary_every=6, lookback_minutes=LOOKBACK)
    s = GrokEmailSource(imap, grok, MagicMock(), lookback_minutes=LOOKBACK, retry_tracker=tracker)
    await s.fetch(5)
    assert repo.get_grok_attempt("1") is None
    assert s.last_not_rendered_count == 1  # 無効のときは従来どおり毎回通知


async def test_no_expiry_warning_when_fetched_in_same_run(repo: RunHistoryRepository) -> None:
    received = NOW - timedelta(minutes=LOOKBACK - GROK_EXPIRY_WARN_LEAD_MINUTES + 10)
    await _source(repo, [_email(received=received)]).fetch(5)  # 失敗を記録
    ok = GrokFetchResult(
        url="u",
        final_url="https://grok.com/c/x",
        html="<html/>",
        body_text="real report body",
        title="t",
        success=True,
    )
    s = _source(repo, [_email(received=received)], grok_result=ok)
    assert len(await s.fetch(5)) == 1
    assert s.last_expiring_count == 0


async def test_expiry_uses_internaldate_and_date_granularity(
    repo: RunHistoryRepository,
) -> None:
    # Date ヘッダは新しいが INTERNALDATE は前日の早朝: 窓の基準は INTERNALDATE
    internal = datetime(2026, 10, 28, 1, 0, tzinfo=UTC)
    msg = _email(received=NOW - timedelta(hours=1)).model_copy(update={"internal_date": internal})
    s = _source(repo, [msg])
    await s.fetch(5)
    assert s.last_expiring_count == 1
