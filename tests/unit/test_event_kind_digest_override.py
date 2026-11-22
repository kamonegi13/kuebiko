"""日報・ニュースレターの形の記事の種別を roundup に上書きする (2026-11-22)。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.eventnews.models import MemberArticle
from src.ui.services import eventnews_hourly_job as job


def _member(body: str, title: str = "The World's Biggest Crypto Heist") -> MemberArticle:
    return MemberArticle(
        article_id="a1",
        title=title,
        url="https://news.example/2026/10/30/heist/",
        feed_title="F",
        feed_url="https://news.example/feed",
        host="news.example",
        importance="medium",
        category="apt",
        status="posted",
        anchor_ts=datetime(2026, 10, 30, tzinfo=UTC),
        summary="s",
        body=body,
        entities=frozenset(),
    )


_DIGEST = "Welcome back to World Brief, where we're looking at a heist, an election and NATO."


def test_mode_off_keeps_llm_kind() -> None:
    assert job._digest_override(_member(_DIGEST), "breach", "m", "0") == ("breach", "m")


def test_mode_on_overrides_digest_to_roundup() -> None:
    got = job._digest_override(_member(_DIGEST), "breach", "m", "1")
    assert got == ("roundup", job.DIGEST_OVERRIDE_MODEL)


def test_shadow_logs_only() -> None:
    assert job._digest_override(_member(_DIGEST), "breach", "m", "shadow") == ("breach", "m")


def test_non_digest_is_untouched() -> None:
    plain = _member("Hackers stole $6 billion from the exchange on Thursday.")
    assert job._digest_override(plain, "breach", "m", "1") == ("breach", "m")


def test_mode_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(job.DIGEST_OVERRIDE_ENV, raising=False)
    assert job.digest_override_mode() == "0"
    monkeypatch.setenv(job.DIGEST_OVERRIDE_ENV, "1")
    assert job.digest_override_mode() == "1"
    monkeypatch.setenv(job.DIGEST_OVERRIDE_ENV, "shadow")
    assert job.digest_override_mode() == "shadow"
