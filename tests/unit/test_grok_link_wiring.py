"""Grok 外部リンク取得の配線 (grok_convert・保存・裏付け関門) のテスト。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.grok.jsonl_parser import TweetEngagement, TweetRecord
from src.grok.link_fetch import LinkedPage, build_link_section
from src.pipeline import grok_convert
from src.pipeline.summary_grounding import ungrounded_reasons
from src.storage.records import ArticleRecord, RunRecord
from src.storage.run_history import RunHistoryRepository
from src.tools.article_model import Article

_PAGE = LinkedPage(
    url="https://t.co/a",
    final_url="https://gov.example/advisory",
    ok=True,
    text="修正版 7.0.2u4 は 2023 年 11 月 22 日に公開された。",
)


def _record(urls: list[str]) -> TweetRecord:
    return TweetRecord(
        tweet_id="9",
        url="https://x.com/t/status/9",
        author_handle="@t",
        posted_at="2026-10-20T03:00:00Z",
        text="ESXi の脆弱性の修正が出た",
        engagement=TweetEngagement(like=10, retweet=5),
        matched_theme="B",
        external_urls=urls,
    )


class _FakeFetcher:
    def __init__(self, pages: tuple[LinkedPage, ...] = (), error: bool = False) -> None:
        self._pages = pages
        self._error = error
        self.calls: list[list[str]] = []

    async def fetch(self, urls: list[str]) -> tuple[LinkedPage, ...]:
        self.calls.append(urls)
        if self._error:
            raise RuntimeError("boom")
        return self._pages


def _article() -> Article:
    return Article(
        id="grok:a:1",
        title="r",
        url="https://grok.com/x",
        summary_html="",
        published=datetime(2026, 10, 20, tzinfo=UTC),
        feed_title="Grok",
        feed_url="https://grok.com/",
    )


def _convert(
    monkeypatch: pytest.MonkeyPatch, record: TweetRecord, fetcher: Any, *, use_links: bool
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def fake_summarize(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        captured["tweet_body"] = args[1]
        raise RuntimeError("stop after capture")  # 機械変換版へ degrade させる

    monkeypatch.setattr(grok_convert, "_summarize_and_build", fake_summarize)
    message, enriched = asyncio.run(
        grok_convert._grok_record_to_message(
            record,
            _article(),
            llm=object(),  # type: ignore[arg-type]
            template=object(),  # type: ignore[arg-type]
            enrichment=None,
            brief_count_24h=0,
            fetcher=fetcher,
            use_links=use_links,
        )
    )
    assert message is not None and enriched is False
    return captured


def test_on_mode_passes_link_text_and_stored_context(monkeypatch: pytest.MonkeyPatch) -> None:
    fetcher = _FakeFetcher((_PAGE,))

    captured = _convert(monkeypatch, _record(["https://t.co/a"]), fetcher, use_links=True)

    assert fetcher.calls == [["https://t.co/a"]]
    assert "https://gov.example/advisory" in captured["link_text"]
    assert "7.0.2u4" in captured["link_text"]
    assert "gov.example" in captured["linked_context"]
    assert "7.0.2u4" not in captured["tweet_body"]  # 投稿の本文は参照先を含まない


def test_shadow_mode_stores_context_but_keeps_summary_input_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _convert(
        monkeypatch, _record(["https://t.co/a"]), _FakeFetcher((_PAGE,)), use_links=False
    )

    assert captured["link_text"] == ""
    assert captured["linked_context"] is not None


def test_off_mode_has_no_fetcher_and_no_context(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _convert(monkeypatch, _record(["https://t.co/a"]), None, use_links=False)

    assert captured["link_text"] == ""
    assert captured["linked_context"] is None


def test_fetch_error_does_not_break_the_tweet(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _convert(
        monkeypatch, _record(["https://t.co/a"]), _FakeFetcher(error=True), use_links=True
    )

    assert captured["link_text"] == ""
    assert captured["linked_context"] is None


def test_tweet_without_external_urls_never_calls_fetcher(monkeypatch: pytest.MonkeyPatch) -> None:
    fetcher = _FakeFetcher((_PAGE,))

    _convert(monkeypatch, _record([]), fetcher, use_links=True)

    assert fetcher.calls == []


# ---------- 裏付け関門: 参照先の本文も照合範囲 ----------

_SUMMARY = "修正版 7.0.2u4 が 2023 年 11 月 22 日に公開された。"


def test_grounding_accepts_numbers_that_come_from_the_linked_page() -> None:
    tweet = "ESXi の脆弱性の修正が出た"

    without = ungrounded_reasons(_SUMMARY, body=tweet)
    with_link = ungrounded_reasons(_SUMMARY, body=tweet + build_link_section((_PAGE,)))

    assert without
    assert with_link == []


# ---------- 保存 ----------


def _repo(tmp_path: Path) -> RunHistoryRepository:
    repo = RunHistoryRepository(db_path=str(tmp_path / "lc.db"))
    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=run_id,
            article_id="g:1",
            title="t",
            url="https://x.com/a/status/1",
            status="posted",
        )
    )
    return repo


def _stored(repo: RunHistoryRepository) -> str | None:
    with repo._connect() as conn:  # noqa: SLF001
        row = conn.execute("SELECT linked_context FROM articles WHERE article_id='g:1'").fetchone()
    return None if row is None else row[0]


def test_set_linked_context_round_trips(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    assert repo.set_linked_context("g:1", '[{"ok": true}]') == 1

    assert _stored(repo) == '[{"ok": true}]'


def test_body_retention_purge_also_clears_linked_context(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    old = datetime.now(UTC) - timedelta(days=200)
    repo.update_article_body("g:1", "tweet", source="grok", fetched_at=old)
    repo.set_linked_context("g:1", '[{"ok": true}]')

    repo.purge_article_bodies_older_than(days=90)

    assert _stored(repo) is None
