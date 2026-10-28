"""巨大な Grok レポートの繰り越しを run_pipeline を通して確かめる (2026-10-29、run 8732)。

- 1 回目: soft deadline で投稿の途中で止まる → レポートは未完了 → メールは既読にしない
- 2 回目: 同じメールを取り直す → 処理済みの投稿は実際の dedup_seen_urls (sqlite) で要約前に
  落ち、残りだけ処理される → メールを既読にする。投稿は各 1 回だけ
- kill (投稿の途中で CancelledError): メールは既読にせず、投稿済みの記事の行は残る
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import jinja2
import pytest

from src.config_loader import PipelineConfig, ProcessorConfig, SourceConfig
from src.grok.fetcher import GrokFetchResult
from src.pipeline import grok_convert
from src.pipeline import orchestrator as orch
from src.pipeline.dispatch import run_then_commit
from src.pipeline.orchestrator import run_pipeline
from src.pipeline.result import PipelineRunResult
from src.storage.run_history import RunHistoryRepository, RunRecord
from src.tools.discord_publisher import DiscordPostMeta, DiscordPublisher
from src.tools.imap_client import EmailMessage
from src.tools.source_router import GrokEmailSource

pytestmark = pytest.mark.integration

_REPORT_URL = "https://grok.com/c/report-1"
_POSTS = 5
#: 要約 1 件で進む時間 (秒)。予算 1000 秒 → 処理に使える 700 秒 → 3 件で止まる
_SECONDS_PER_POST = 300.0
_BUDGET = 1000.0


def _jsonl(*, with_ids: bool = True) -> str:
    lines = [
        json.dumps(
            {
                "tweet_id": str(1000 + i) if with_ids else "",
                "url": f"https://x.com/analyst{i}/status/{1000 + i}" if with_ids else "",
                "author_handle": f"@analyst{i}",
                "posted_at": "2026-10-28T19:00:00Z",
                "text": f"Distinct observation number {i} about campaign {i * 7}",
                "matched_theme": "B",
                "account_class": "analyst_known",
            }
        )
        for i in range(_POSTS)
    ]
    return "\n".join(lines)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


class _AnyChannel(dict[str, DiscordPublisher]):
    """どのチャンネルにも同じ publisher を返す (routing の詳細はこのテストの対象外)。"""

    def __init__(self, publisher: DiscordPublisher) -> None:
        super().__init__()
        self._publisher = publisher

    def get(self, key: str, default: Any = None) -> DiscordPublisher:
        return self._publisher


def _source(body: str | None = None) -> tuple[GrokEmailSource, AsyncMock]:
    imap = AsyncMock()
    imap.fetch_unread.return_value = [
        EmailMessage(
            uid="42",
            subject="Grok report",
            sender="noreply@x.ai",
            received_at=datetime(2026, 10, 28, 19, tzinfo=UTC),
            body_text="",
            body_html="",
            extracted_urls=[_REPORT_URL],
        )
    ]
    imap.mark_seen.side_effect = lambda uids: len(uids)
    grok = AsyncMock()
    grok.fetch.return_value = GrokFetchResult(
        url=_REPORT_URL,
        final_url=_REPORT_URL,
        html="<html></html>",
        title="Grok report",
        success=True,
        body_text=_jsonl() if body is None else body,
    )
    return GrokEmailSource(imap, grok, MagicMock(), mark_as_read=True), imap


@pytest.fixture
def template() -> jinja2.Template:
    env = jinja2.Environment(loader=jinja2.DictLoader({"t.j2": "{{ article.title }}"}))
    return env.get_template("t.j2")


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(orch, "time", fake)
    return fake


@pytest.fixture
def summarized(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> list[str]:
    """要約の呼び出しを記録し、時計を進めて機械変換版に落とす (LLM を使わない)。"""
    calls: list[str] = []

    async def fake_summarize(article: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(article.url)
        clock.now += _SECONDS_PER_POST
        raise RuntimeError("use machine conversion")

    monkeypatch.setattr(grok_convert, "_summarize_and_build", fake_summarize)
    monkeypatch.setattr(grok_convert, "link_fetch_mode", lambda: "off")
    # 機械変換版は見出しが似るため投稿直前の内容重複に当たる。このテストは投稿の回数を数える
    monkeypatch.setattr(orch, "check_pre_post_dedup", lambda *a, **k: None)
    return calls


def _repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "carry.db")


def _start(repo: RunHistoryRepository) -> int:
    return repo.start_run(
        RunRecord(
            started_at=datetime(2026, 10, 28, 20, tzinfo=UTC),
            pipeline="grok-briefing",
            dry_run=False,
            triggered_by="scheduler",
        )
    )


async def _run(
    source: GrokEmailSource,
    repo: RunHistoryRepository,
    publisher: DiscordPublisher,
    template: jinja2.Template,
    run_id: int,
    budget: float | None,
) -> PipelineRunResult:
    pipeline = PipelineConfig(
        name="grok-briefing",
        source=SourceConfig(type="grok_email", max_articles=15),
        processor=ProcessorConfig(),
    )
    return await run_then_commit(
        source,
        lambda: run_pipeline(
            config=cast(Any, object()),
            pipeline=pipeline,
            source=source,
            extractor=cast(Any, AsyncMock()),
            llm=cast(Any, AsyncMock()),
            publishers=_AnyChannel(publisher),
            template=template,
            dry_run=False,
            dedup_repo=repo,
            run_id=run_id,
            time_budget_seconds=budget,
        ),
    )


def _publisher() -> DiscordPublisher:
    pub = AsyncMock(spec=DiscordPublisher)
    pub.post.return_value = DiscordPostMeta(message_id="m", channel_id="c")
    return cast(DiscordPublisher, pub)


@pytest.mark.asyncio
async def test_large_report_is_split_across_runs_without_loss_or_duplicates(
    tmp_path: Path, template: jinja2.Template, clock: _Clock, summarized: list[str]
) -> None:
    repo = _repo(tmp_path)
    publisher = _publisher()

    source, imap = _source()
    first = await _run(source, repo, publisher, template, _start(repo), _BUDGET)

    assert len(summarized) == 3  # 4 件目の前で soft deadline
    assert first.unfinished_article_ids  # レポートは未完了
    imap.mark_seen.assert_not_called()  # → メールは未読のまま

    clock.now = 0.0
    source2, imap2 = _source()  # 次の run が同じメールを取り直す
    second = await _run(source2, repo, publisher, template, _start(repo), _BUDGET)

    assert second.unfinished_article_ids == ()
    imap2.mark_seen.assert_awaited_once_with(["42"])
    # 要約は全体で各投稿 1 回だけ (処理済みの 3 件は実 DB の既知判定で要約前に落ちた)
    assert sorted(summarized) == sorted(set(summarized))
    assert len(summarized) == _POSTS
    posted = [
        c.args[0].metadata["tweet_id"]
        for c in cast(AsyncMock, publisher.post).call_args_list
        if "tweet_id" in c.args[0].metadata  # 稼働通知 (ops) を除く
    ]
    assert len(posted) == _POSTS
    assert len(set(posted)) == _POSTS
    rows = repo.list_articles(limit=50)
    assert len({r.article_id for r in rows}) == len(rows) == _POSTS


@pytest.mark.asyncio
async def test_kill_mid_posting_keeps_email_unseen_and_posted_rows(
    tmp_path: Path, template: jinja2.Template, clock: _Clock, summarized: list[str]
) -> None:
    repo = _repo(tmp_path)
    pub = AsyncMock(spec=DiscordPublisher)
    pub.post.side_effect = [
        DiscordPostMeta(message_id="m", channel_id="c"),
        asyncio.CancelledError(),
    ]
    source, imap = _source()
    run_id = _start(repo)

    with pytest.raises(asyncio.CancelledError):
        await _run(source, repo, cast(DiscordPublisher, pub), template, run_id, None)

    imap.mark_seen.assert_not_called()
    rows = repo.list_articles(run_id=run_id)
    # 投稿できた 1 件は既読の登録と同時に行が書かれている (末尾の保存を待たない)
    assert [r.status for r in rows] == ["posted"]


@pytest.mark.asyncio
async def test_posts_without_id_or_url_are_dropped_on_the_next_run(
    tmp_path: Path, template: jinja2.Template, clock: _Clock, summarized: list[str]
) -> None:
    """tweet id も URL も無い投稿も、本文と投稿者の hash で取り直しても同じ鍵になる。"""
    repo = _repo(tmp_path)
    publisher = _publisher()
    body = _jsonl(with_ids=False)

    source, imap = _source(body)
    first = await _run(source, repo, publisher, template, _start(repo), _BUDGET)
    assert len(summarized) == 3
    assert first.unfinished_article_ids
    imap.mark_seen.assert_not_called()

    clock.now = 0.0
    source2, imap2 = _source(body)
    second = await _run(source2, repo, publisher, template, _start(repo), _BUDGET)

    assert len(summarized) == _POSTS  # 2 回目は残りの 2 件だけ要約した
    assert second.unfinished_article_ids == ()
    imap2.mark_seen.assert_awaited_once_with(["42"])
    rows = repo.list_articles(limit=50)
    assert len({r.article_id for r in rows}) == len(rows) == _POSTS
