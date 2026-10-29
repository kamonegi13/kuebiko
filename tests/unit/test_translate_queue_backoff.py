"""翻訳キューの先頭詰まり対策 (P0-24): 失敗の印・バックオフ・原文写し判定の計測。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.cti.body_translator import (
    TranslationEchoError,
    broken_translation_reason,
    find_echo_match,
)
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord
from src.tools.llm_client import LLMClient, LLMError, LLMResponse
from src.ui.services.body_translate_backlog import run_body_translate_backlog

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "q.db")


def _add(repo: RunHistoryRepository, aid: str, body: str, age_minutes: int = 0) -> None:
    rid = repo.start_run(RunRecord(started_at=NOW, pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=rid,
            article_id=aid,
            title=f"t-{aid}",
            url=f"u-{aid}",
            status="posted",
            created_at=datetime.now(UTC) - timedelta(minutes=age_minutes),
        )
    )
    repo.update_article_body(aid, body)


def _fail(repo: RunHistoryRepository, aid: str, times: int, when: datetime) -> None:
    for _ in range(times):
        repo.record_translate_failure(aid, "echo", when=when)


def test_default_listing_ignores_attempts(repo: RunHistoryRepository) -> None:
    _add(repo, "a", "body")
    _fail(repo, "a", 4, datetime.now(UTC))
    assert repo.list_articles_untranslated() == ["a"]


def test_three_failures_within_24h_are_skipped(repo: RunHistoryRepository) -> None:
    _add(repo, "bad", "body", age_minutes=1)
    _add(repo, "ok", "body", age_minutes=10)
    _fail(repo, "bad", 3, NOW - timedelta(hours=1))
    assert repo.list_articles_untranslated(backoff=True, now=NOW) == ["ok"]


def test_two_failures_still_selected(repo: RunHistoryRepository) -> None:
    _add(repo, "a", "body")
    _fail(repo, "a", 2, NOW)
    assert repo.list_articles_untranslated(backoff=True, now=NOW) == ["a"]


def test_returns_after_backoff_window(repo: RunHistoryRepository) -> None:
    _add(repo, "a", "body")
    _fail(repo, "a", 3, NOW - timedelta(hours=25))
    assert repo.list_articles_untranslated(backoff=True, now=NOW) == ["a"]


def test_backoff_grows_with_fail_count(repo: RunHistoryRepository) -> None:
    _add(repo, "a", "body")
    _fail(repo, "a", 4, NOW - timedelta(hours=48))
    assert repo.list_articles_untranslated(backoff=True, now=NOW) == []
    _fail(repo, "a", 0, NOW)
    assert repo.list_articles_untranslated(backoff=True, now=NOW + timedelta(hours=30)) == ["a"]


def test_fifth_failure_waits_seven_days(repo: RunHistoryRepository) -> None:
    _add(repo, "a", "body")
    _fail(repo, "a", 5, NOW - timedelta(days=6))
    assert repo.list_articles_untranslated(backoff=True, now=NOW) == []
    assert repo.list_articles_untranslated(backoff=True, now=NOW + timedelta(days=2)) == ["a"]


def test_over_cap_is_never_selected(repo: RunHistoryRepository) -> None:
    _add(repo, "a", "body")
    _fail(repo, "a", 6, NOW - timedelta(days=60))
    assert repo.list_articles_untranslated(backoff=True, now=NOW + timedelta(days=30)) == []
    # 記録だけは残り body_ja は NULL のまま
    attempt = repo.get_translate_attempt("a")
    assert attempt is not None and attempt[0] == 6
    assert repo.get_article_body_ja("a") is None


def test_clear_removes_mark(repo: RunHistoryRepository) -> None:
    _add(repo, "a", "body")
    _fail(repo, "a", 3, NOW)
    repo.clear_translate_attempts("a")
    assert repo.get_translate_attempt("a") is None


def test_purge_removes_old_rows(repo: RunHistoryRepository) -> None:
    _fail(repo, "old", 1, datetime.now(UTC) - timedelta(days=100))
    _fail(repo, "new", 1, datetime.now(UTC))
    assert repo.purge_translate_attempts(days=90) == 1
    assert repo.get_translate_attempt("old") is None
    assert repo.get_translate_attempt("new") is not None


class _EchoLLM(LLMClient):
    """入力をそのまま返す (写しの判定に当たる)。"""

    def __init__(self, raise_on: str | None = None, ok_on: str | None = None) -> None:
        self._raise_on = raise_on
        self._ok_on = ok_on

    @property
    def model(self) -> str:
        return "fake"

    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        think: bool | None = None,
    ) -> LLMResponse:
        if self._raise_on and self._raise_on in prompt:
            raise LLMError("boom")
        if self._ok_on and self._ok_on in prompt:
            return LLMResponse(text="訳文です", model="fake")
        return LLMResponse(text=prompt, model="fake")

    async def generate_structured(self, *a: Any, **kw: Any) -> Any:
        raise NotImplementedError


_ENGLISH = "The quick brown fox jumps over the lazy dog while the attackers exfiltrate data. " * 2


async def test_job_records_failure_and_skips_after_three(
    repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRANSLATE_QUEUE_BACKOFF", "1")
    _add(repo, "stuck", _ENGLISH, age_minutes=1)
    _add(repo, "later", "short body", age_minutes=30)

    for _ in range(3):
        stats = await run_body_translate_backlog(repo, _EchoLLM(ok_on="short body"))
        assert stats["failed"] == 1
    attempt = repo.get_translate_attempt("stuck")
    assert attempt is not None and attempt[0] == 3
    _add(repo, "later2", "short body 2", age_minutes=40)
    stats = await run_body_translate_backlog(repo, _EchoLLM(ok_on="short body"))
    assert stats["picked"] == 1  # stuck は外れ、未訳の later2 だけ選ばれる
    assert stats["translated"] == 1 and stats["echo_failed"] == 0


async def test_job_success_clears_mark(
    repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRANSLATE_QUEUE_BACKOFF", "1")
    _add(repo, "a", "plain body")
    repo.record_translate_failure("a", "x", when=datetime.now(UTC) - timedelta(days=2))

    class _Ok(_EchoLLM):
        async def generate(
            self,
            prompt: str,
            system: str | None = None,
            temperature: float = 0.3,
            max_tokens: int = 4096,
            think: bool | None = None,
        ) -> LLMResponse:
            return LLMResponse(text="訳文です", model="fake")

    await run_body_translate_backlog(repo, _Ok())
    assert repo.get_translate_attempt("a") is None


async def test_job_flag_off_keeps_old_selection(
    repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRANSLATE_QUEUE_BACKOFF", "0")
    _add(repo, "a", _ENGLISH)
    _fail(repo, "a", 4, datetime.now(UTC))
    stats = await run_body_translate_backlog(repo, _EchoLLM())
    assert stats["picked"] == 1


async def test_job_survives_record_failure(
    repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRANSLATE_QUEUE_BACKOFF", "1")
    _add(repo, "a", _ENGLISH)

    def _boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(repo, "record_translate_failure", _boom)
    stats = await run_body_translate_backlog(repo, _EchoLLM())
    assert stats["failed"] == 1
    assert stats["attempt_record_errors"] == 1


def test_find_echo_match_returns_lengths_only() -> None:
    m = find_echo_match(_ENGLISH, _ENGLISH)
    assert m is not None
    assert m.paragraph_chars == len(_ENGLISH.strip())
    assert 0.0 < m.ascii_ratio <= 1.0
    assert not hasattr(m, "text")


def test_find_echo_match_none_for_translation() -> None:
    assert find_echo_match(_ENGLISH, "これは訳文です。") is None
    assert broken_translation_reason(_ENGLISH, "これは訳文です。") is None


def test_echo_error_is_llm_error() -> None:
    assert issubclass(TranslationEchoError, LLMError)
