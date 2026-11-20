"""triage の判定の使い回し (2026-11-20、11-19 監査 D4 O1 の triage 部分)。

繰り越した記事を毎 run 判定し直し、triage の呼び出しの 3 割強が繰り返しだった。鍵 (URL・
モデル・プロンプト全文)・TTL・失敗した判定を記録しないこと・旗ごとの振る舞いを固定する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import src.pipeline.triage_reuse as reuse_mod
from src.pipeline.filters import _filter_by_triage
from src.pipeline.triage_reuse import TriageDecisionCache, decision_key
from src.storage.run_history import RunHistoryRepository
from src.tools.article_model import Article
from src.tools.article_triage import TriageDecision

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "t.db")


def test_key_changes_with_prompt_and_model() -> None:
    base = decision_key("https://e/a", "m1", "prompt")

    assert base == decision_key("https://e/a", "m1", "prompt")
    assert base != decision_key("https://e/a", "m1", "prompt (基準を変えた)")
    assert base != decision_key("https://e/a", "m2", "prompt")


def test_cached_decision_is_returned_within_ttl(repo: RunHistoryRepository) -> None:
    TriageDecisionCache(repo, now=NOW).put("k", TriageDecision(importance="high", reason="r"))

    later = TriageDecisionCache(repo, now=NOW + timedelta(hours=23))

    assert later.get("k") == TriageDecision(importance="high", reason="r")


def test_expired_decision_is_not_used(repo: RunHistoryRepository) -> None:
    TriageDecisionCache(repo, now=NOW).put("k", TriageDecision(importance="high", reason="r"))

    assert TriageDecisionCache(repo, now=NOW + timedelta(hours=25)).get("k") is None


def test_fail_open_decisions_are_not_recorded(repo: RunHistoryRepository) -> None:
    cache = TriageDecisionCache(repo, now=NOW)

    cache.put("k", TriageDecision(importance="medium", reason="triage error", error=True))

    assert cache.get("k") is None


def _article() -> Article:
    return Article(
        id="a1",
        title="Example vendor patches a flaw",
        url="https://example.com/a1",
        summary_html="<p>The vendor released a fix.</p>",
        published=NOW,
        feed_title="Feed",
        feed_url="https://example.com/feed",
    )


async def _run_twice(monkeypatch: pytest.MonkeyPatch, repo: RunHistoryRepository) -> AsyncMock:
    monkeypatch.setattr(reuse_mod, "RunHistoryRepository", lambda: repo)
    monkeypatch.setenv("TRIAGE_GEO_RESCUE", "0")
    monkeypatch.setenv("TRIAGE_SHADOW_PER_RUN", "0")  # M4 の影の判定は数に入れない
    llm = AsyncMock()
    llm.model = "m1"
    llm.generate_structured = AsyncMock(
        return_value=TriageDecision(importance="medium", reason="x")
    )
    for _ in range(2):
        await _filter_by_triage([_article()], llm, keep_importance={"high", "medium"}, max_keep=5)
    return llm


@pytest.mark.asyncio
async def test_on_skips_the_llm_for_a_carried_over_article(
    monkeypatch: pytest.MonkeyPatch, repo: RunHistoryRepository
) -> None:
    monkeypatch.setenv("TRIAGE_DECISION_REUSE", "1")

    llm = await _run_twice(monkeypatch, repo)

    assert llm.generate_structured.await_count == 1


@pytest.mark.asyncio
async def test_shadow_still_calls_the_llm_every_time(
    monkeypatch: pytest.MonkeyPatch, repo: RunHistoryRepository
) -> None:
    monkeypatch.setenv("TRIAGE_DECISION_REUSE", "shadow")

    llm = await _run_twice(monkeypatch, repo)

    assert llm.generate_structured.await_count == 2


@pytest.mark.asyncio
async def test_off_never_touches_the_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRIAGE_DECISION_REUSE", raising=False)

    def _fail() -> RunHistoryRepository:
        raise AssertionError("DB に触れた")

    monkeypatch.setattr(reuse_mod, "RunHistoryRepository", _fail)
    monkeypatch.setenv("TRIAGE_GEO_RESCUE", "0")
    llm = AsyncMock()
    llm.generate_structured = AsyncMock(return_value=TriageDecision(importance="low", reason="x"))

    await _filter_by_triage([_article()], llm, keep_importance={"high"}, max_keep=5)
