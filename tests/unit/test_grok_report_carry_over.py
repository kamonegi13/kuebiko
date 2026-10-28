"""巨大な Grok レポートを複数の run に分けて処理し、何も失わないことのテスト (2026-10-29)。

背景 (run 8732): 392 行のレポートの投稿を 1 件 ~15 秒で要約し続け、段の上限で子が kill
された。通知メールは取得直後に既読化済みで、記事の保存は run の最後のため、投稿 ~280 件が
消えた。修正は 3 点:
1. 投稿ごとに soft deadline を見て、残りを ``deferred`` として返す
2. 要約の前に run 横断の既知判定で処理済みの投稿を落とす (繰り越し先で要約し直さない)
3. 通知メールは成果の確定後に、未完了の記事を含まないものだけ既読化する
"""

from __future__ import annotations

import asyncio
import imaplib
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.grok.fetcher import GrokFetchResult
from src.grok.jsonl_parser import TweetEngagement, TweetRecord
from src.grok.report_url import is_grok_report_url
from src.pipeline import grok_convert
from src.pipeline.dispatch import commit_source_seen
from src.pipeline.grok_convert import GrokExpansion, drop_seen_records
from src.pipeline.orchestrator import _ArticleWork, unfinished_article_ids
from src.pipeline.result import PipelineRunResult
from src.tools.article_model import Article
from src.tools.discord_publisher import BriefingMessage
from src.tools.imap_client import EmailMessage, ImapClient
from src.tools.source_router import GrokEmailSource
from src.tools.url_normalizer import url_hash


def _record(i: int, external: list[str] | None = None) -> TweetRecord:
    return TweetRecord(
        tweet_id=str(i),
        url=f"https://x.com/a/status/{i}",
        author_handle="@a",
        posted_at="2026-10-28T20:00:00Z",
        text=f"post {i}",
        engagement=TweetEngagement(like=10, retweet=5),
        matched_theme="B",
        external_urls=external or [],
    )


class _Seen:
    def __init__(self, urls: Sequence[str]) -> None:
        self._seen = {url_hash(u) for u in urls}

    def __call__(self, hashes: Sequence[str]) -> set[str]:
        return {h for h in hashes if h in self._seen}


def _article(article_id: str = "grok:r:1") -> Article:
    return Article(
        id=article_id,
        title="r",
        url="https://grok.com/c/r",
        summary_html="",
        published=datetime(2026, 10, 28, tzinfo=UTC),
        feed_title="Grok",
        feed_url="https://grok.com/",
    )


# ---------- 1. 要約の前の既知判定 ----------


def test_drop_seen_records_by_tweet_url_or_dedup_key() -> None:
    records = [_record(1), _record(2, ["https://ext.example/a"]), _record(3)]
    # 1 = 投稿 URL が既知 (前の run で処理済み) / 2 = 外部 URL (dedup_key) が既知
    seen = _Seen(["https://x.com/a/status/1", "https://ext.example/a"])
    kept, dropped = drop_seen_records(records, seen)
    assert [r.tweet_id for r in kept] == ["3"]
    assert dropped == 2


def test_drop_seen_records_without_filter_passes_through() -> None:
    records = [_record(1)]
    assert drop_seen_records(records, None) == (records, 0)


# ---------- 2. 投稿ごとの時間切れ ----------


def _patch_expansion(monkeypatch: pytest.MonkeyPatch, records: list[TweetRecord]) -> list[str]:
    import src.grok.jsonl_parser as parser

    parsed = MagicMock(records=records, total_lines=len(records), parsed_count=len(records))
    parsed.skipped_lines = []
    monkeypatch.setattr(parser, "parse_jsonl", lambda body: parsed)
    monkeypatch.setattr(parser, "filter_records", lambda recs: list(recs))
    monkeypatch.setattr(grok_convert, "link_fetch_mode", lambda: "off")
    summarized: list[str] = []

    async def fake_to_message(record: TweetRecord, *args: Any, **kwargs: Any) -> Any:
        summarized.append(record.tweet_id)
        msg = BriefingMessage(title="t", summary="s", importance="low", category="other")
        return msg, True

    monkeypatch.setattr(grok_convert, "_grok_record_to_message", fake_to_message)
    return summarized


@pytest.mark.asyncio
async def test_stops_between_posts_and_reports_deferred(monkeypatch: pytest.MonkeyPatch) -> None:
    summarized = _patch_expansion(monkeypatch, [_record(i) for i in range(5)])
    calls = iter([False, False, True, True, True])

    result = await grok_convert._grok_jsonl_to_briefings(
        "{}",
        _article(),
        llm=cast(Any, object()),
        template=cast(Any, object()),
        should_stop=lambda: next(calls),
    )

    assert summarized == ["0", "1"]
    assert len(result.briefings) == 2
    assert result.deferred == 3
    assert not result.is_complete


@pytest.mark.asyncio
async def test_next_run_resumes_without_resummarizing(monkeypatch: pytest.MonkeyPatch) -> None:
    summarized = _patch_expansion(monkeypatch, [_record(i) for i in range(5)])
    # 前の run で 0 と 1 を処理済み (投稿 URL が既読に登録されている)
    seen = _Seen(["https://x.com/a/status/0", "https://x.com/a/status/1"])

    result = await grok_convert._grok_jsonl_to_briefings(
        "{}",
        _article(),
        llm=cast(Any, object()),
        template=cast(Any, object()),
        should_stop=lambda: False,
        seen_filter=seen,
    )

    assert summarized == ["2", "3", "4"]
    assert result.is_complete


# ---------- 3. 未完了の記事 ----------


def test_unfinished_ids_are_not_started_and_partial_reports() -> None:
    articles = [_article("a"), _article("b"), _article("c")]
    works = [
        _ArticleWork.empty("a"),
        _ArticleWork(
            briefings=(),
            outcomes=(),
            errors=(),
            grok_subarticles=(),
            article_id="b",
            deferred_posts=4,
        ),
    ]  # c は時間切れで着手していない
    assert unfinished_article_ids(articles, works) == ("b", "c")


def test_expansion_complete_by_default() -> None:
    assert GrokExpansion(briefings=[]).is_complete


# ---------- 4. 既読化は成果の確定後 ----------


def _email(uid: str, url: str) -> EmailMessage:
    return EmailMessage(
        uid=uid,
        subject="Grok report",
        sender="noreply@x.ai",
        received_at=datetime(2026, 10, 28, tzinfo=UTC),
        body_text="",
        body_html="",
        extracted_urls=[url],
    )


async def _fetched_source(monkeypatch: pytest.MonkeyPatch) -> tuple[GrokEmailSource, Any]:
    imap = AsyncMock()
    imap.fetch_unread.return_value = [
        _email("10", "https://grok.com/c/one"),
        _email("11", "https://grok.com/c/two"),
    ]
    imap.mark_seen.side_effect = lambda uids: len(uids)
    grok = AsyncMock()
    grok.fetch.side_effect = lambda url: GrokFetchResult(
        url=url,
        final_url=url,
        html="<html></html>",
        title="R",
        success=True,
        body_text="report body",
    )
    source = GrokEmailSource(imap, grok, MagicMock(), mark_as_read=True)
    await source.fetch(max_count=5)
    return source, imap


@pytest.mark.asyncio
async def test_fetch_does_not_mark_seen(monkeypatch: pytest.MonkeyPatch) -> None:
    _source, imap = await _fetched_source(monkeypatch)
    imap.mark_seen.assert_not_called()


@pytest.mark.asyncio
async def test_commit_keeps_email_of_unfinished_report_unseen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, imap = await _fetched_source(monkeypatch)
    articles = await source.fetch(max_count=5)
    unfinished = [a.id for a in articles if a.url.endswith("/two")]

    marked = await source.commit_seen(unfinished)

    assert marked == 1
    imap.mark_seen.assert_awaited_once_with(["10"])
    # 2 回目は何もしない (取得ごとに 1 回だけ)
    assert await source.commit_seen(()) == 0


@pytest.mark.asyncio
async def test_dispatch_commit_passes_unfinished_ids() -> None:
    source = MagicMock()
    source.commit_seen = AsyncMock(return_value=2)
    result = PipelineRunResult(unfinished_article_ids=("x",))
    assert await commit_source_seen(source, result) == 2
    source.commit_seen.assert_awaited_once_with(("x",))


@pytest.mark.asyncio
async def test_dispatch_commit_ignores_sources_without_commit_and_errors() -> None:
    assert await commit_source_seen(object(), PipelineRunResult()) == 0
    failing = MagicMock()
    failing.commit_seen = AsyncMock(side_effect=RuntimeError("imap down"))
    assert await commit_source_seen(failing, PipelineRunResult()) == 0


# ---------- 5. 既読化の時点で切れた IMAP 接続 ----------


@pytest.mark.asyncio
async def test_mark_seen_reconnects_once_after_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ImapClient(user="u", password="p")
    conn = MagicMock()
    conn.uid.return_value = ("OK", [])
    monkeypatch.setattr(client, "_connect_sync", lambda: conn)
    client._conn = MagicMock()
    client._conn.uid.side_effect = imaplib.IMAP4.abort("socket error: EOF")

    assert await client.mark_seen(["1", "2"]) == 2
    assert conn.uid.call_count == 2  # 繋ぎ直した接続で 2 件とも既読化


@pytest.mark.asyncio
async def test_mark_seen_gives_up_when_reconnect_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ImapClient(user="u", password="p")

    def refuse() -> Any:
        raise OSError("connection refused")

    monkeypatch.setattr(client, "_connect_sync", refuse)
    client._conn = MagicMock()
    client._conn.uid.side_effect = imaplib.IMAP4.abort("socket error: EOF")

    assert await client.mark_seen(["1"]) == 0  # 未読に残る = 次の run が取り直す


# ---------- 6. レビュー指摘 (2026-10-29) ----------


@pytest.mark.asyncio
async def test_dispatch_commit_skipped_on_dry_run() -> None:
    source = MagicMock()
    source.commit_seen = AsyncMock(return_value=1)
    assert await commit_source_seen(source, PipelineRunResult(dry_run=True)) == 0
    source.commit_seen.assert_not_called()


@pytest.mark.parametrize(
    ("url", "tweet_id", "expected"),
    [
        ("https://twitter.com/a/status/1", "1", "https://x.com/a/status/1"),
        ("https://mobile.x.com/a/status/1", "1", "https://x.com/a/status/1"),
        ("https://www.x.com/a/status/1", "1", "https://x.com/a/status/1"),
        ("https://example.com/post", "77", "https://x.com/i/status/77"),
        ("", "", ""),
        ("https://grok.com/c/r", "", ""),  # 親レポートの URL は投稿の識別子にしない
    ],
)
def test_canonical_post_url(url: str, tweet_id: str, expected: str) -> None:
    assert grok_convert.canonical_post_url(url, tweet_id) == expected


def test_subarticle_source_never_returns_parent_identity() -> None:
    parent = _article()
    alias = grok_convert.grok_subarticle_source(parent, "https://twitter.com/a/status/5")
    assert alias.url == "https://x.com/a/status/5"
    assert alias.feed_url == "https://x.com/a"
    by_id = grok_convert.grok_subarticle_source(parent, "https://t.co/zzz", tweet_id="6")
    assert by_id.url == "https://x.com/i/status/6"
    bare = grok_convert.grok_subarticle_source(parent, "", fallback_key="grok:r:1#i3")
    assert bare.url == "https://grok.com/post/grok%3Ar%3A1%23i3"
    # 断片は正規化で落ちるため、どれも親と別の hash になること
    parent_hash = url_hash(parent.url)
    assert parent_hash not in {url_hash(a.url) for a in (alias, by_id, bare)}
    assert not is_grok_report_url(bare.url)


def test_seen_registration_and_lookup_use_the_same_key() -> None:
    # twitter.com の投稿を処理済みとして登録した URL で、次の run の要約前の照合が当たる
    record = _record(8).model_copy(update={"url": "https://twitter.com/a/status/8"})
    registered = grok_convert.grok_subarticle_source(_article(), record.url, tweet_id="8").url
    kept, dropped = drop_seen_records([record], _Seen([registered]))
    assert (kept, dropped) == ([], 1)


def test_is_grok_report_url() -> None:
    assert is_grok_report_url("https://grok.com/c/abc?rid=1")
    assert is_grok_report_url("https://grok.com/chat/abc")
    assert not is_grok_report_url("https://grok.com/automations?automationId=1")
    assert not is_grok_report_url("https://example.com/c/abc")


def test_url_dedup_keeps_known_grok_report_but_drops_known_task_page(tmp_path: Any) -> None:
    from src.pipeline.filters import _filter_duplicates
    from src.storage.run_history import RunHistoryRepository

    repo = RunHistoryRepository(db_path=tmp_path / "dedup.db")
    report = _article("grok:r:1").model_copy(update={"url": "https://grok.com/c/r"})
    task = _article("grok:t:1").model_copy(
        update={"url": "https://grok.com/automations?automationId=1"}
    )
    for a in (report, task):
        repo.mark_url_seen(url_hash=url_hash(a.url), url=a.url, article_id=a.id, title="")

    survivors, skipped, _ids = _filter_duplicates([report, task], repo)

    assert [a.id for a in survivors] == ["grok:r:1"]
    assert skipped == 1


def test_unfinished_ids_include_transient_failures() -> None:
    works = [
        _ArticleWork(
            briefings=(),
            outcomes=(),
            errors=(),
            grok_subarticles=(),
            article_id="a",
            retry=True,
        )
    ]
    assert unfinished_article_ids([_article("a")], works) == ("a",)


def _source_with(results: dict[str, GrokFetchResult], urls: list[str]) -> tuple[Any, Any]:
    imap = AsyncMock()
    imap.fetch_unread.return_value = [
        EmailMessage(
            uid="20",
            subject="Grok report",
            sender="noreply@x.ai",
            received_at=datetime(2026, 10, 28, tzinfo=UTC),
            body_text="",
            body_html="",
            extracted_urls=urls,
        )
    ]
    imap.mark_seen.side_effect = lambda uids: len(uids)
    grok = AsyncMock()
    grok.fetch.side_effect = lambda url: results[url]
    return GrokEmailSource(imap, grok, MagicMock(), mark_as_read=True), imap


def _ok(url: str, body: str = "report body") -> GrokFetchResult:
    return GrokFetchResult(
        url=url, final_url=url, html="<html></html>", title="R", success=True, body_text=body
    )


def _failed(url: str, reason: str) -> GrokFetchResult:
    return GrokFetchResult(
        url=url, final_url=url, html="", title="", success=False, failure_reason=reason
    )


@pytest.mark.asyncio
async def test_email_with_a_retryable_url_failure_stays_unseen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b = "https://grok.com/c/one", "https://grok.com/c/two"
    source, imap = _source_with({a: _ok(a), b: _failed(b, "timeout: x")}, [a, b])
    articles = await source.fetch(max_count=5)
    assert len(articles) == 1
    assert await source.commit_seen(()) == 0
    imap.mark_seen.assert_not_called()


@pytest.mark.asyncio
async def test_email_cut_by_max_count_stays_unseen() -> None:
    a, b = "https://grok.com/c/one", "https://grok.com/c/two"
    source, imap = _source_with({a: _ok(a), b: _ok(b)}, [a, b])
    assert len(await source.fetch(max_count=1)) == 1
    assert await source.commit_seen(()) == 0


@pytest.mark.asyncio
async def test_permanent_failures_and_empty_pages_do_not_block_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = "https://grok.com/c/one"
    icon = "https://grok.com/icon-192x192.png"
    gone = "https://grok.com/c/gone"
    monkeypatch.setattr(
        "src.tools.source_router._trafilatura_extract", lambda html, **kw: "", raising=False
    )
    source, imap = _source_with(
        {a: _ok(a), icon: _ok(icon, body=""), gone: _failed(gone, "http_error_404")},
        [icon, a, gone],
    )
    assert len(await source.fetch(max_count=5)) == 1
    assert await source.commit_seen(()) == 1
    imap.mark_seen.assert_awaited_once_with(["20"])


@pytest.mark.asyncio
async def test_reconnect_logs_out_the_dead_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ImapClient(user="u", password="p")
    fresh = MagicMock()
    fresh.uid.return_value = ("OK", [])
    monkeypatch.setattr(client, "_connect_sync", lambda: fresh)
    dead = MagicMock()
    dead.uid.side_effect = imaplib.IMAP4.abort("EOF")
    client._conn = dead

    assert await client.mark_seen(["1"]) == 1
    dead.logout.assert_called_once()


@pytest.mark.asyncio
async def test_run_then_commit_commits_only_after_success() -> None:
    from src.pipeline.dispatch import run_then_commit

    source = MagicMock()
    source.commit_seen = AsyncMock(return_value=1)

    async def ok() -> PipelineRunResult:
        return PipelineRunResult(unfinished_article_ids=("u",))

    async def killed() -> PipelineRunResult:
        raise asyncio.CancelledError

    assert (await run_then_commit(source, ok)).unfinished_article_ids == ("u",)
    source.commit_seen.assert_awaited_once_with(("u",))
    source.commit_seen.reset_mock()
    with pytest.raises(asyncio.CancelledError):
        await run_then_commit(source, killed)
    source.commit_seen.assert_not_called()
