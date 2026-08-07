"""バックログ翻訳ジョブ (src/ui/services/body_translate_backlog.py) のテスト。

新しい順の消化 / 1 件失敗の継続 / 時間予算打切り / 未訳クエリを検証。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeVar

import pytest
from pydantic import BaseModel

from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord
from src.tools.llm_client import LLMClient, LLMError, LLMResponse
from src.ui.services.body_translate_backlog import run_body_translate_backlog

_T = TypeVar("_T", bound=BaseModel)


class FakeLLM(LLMClient):
    """本文ごとに決め打ち応答。fail_on に含まれる本文は失敗させる。"""

    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self._fail_on = fail_on or set()

    @property
    def model(self) -> str:
        return "fake-model"

    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        think: bool | None = None,
    ) -> LLMResponse:
        self.calls.append(prompt)
        for marker in self._fail_on:
            if marker in prompt:
                raise LLMError("simulated failure")
        return LLMResponse(text=f"訳-{len(self.calls)}", model="fake-model")

    async def generate_structured(
        self,
        prompt: str,
        schema: type[_T],
        system: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        think: bool | None = None,
        max_attempts: int = 3,
    ) -> _T:
        raise NotImplementedError


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "backlog.db")


def _add(
    repo: RunHistoryRepository,
    aid: str,
    body: str | None,
    *,
    age_minutes: int = 0,
) -> None:
    rid = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=rid,
            article_id=aid,
            title=f"t-{aid}",
            url=f"u-{aid}",
            status="posted",
            created_at=datetime.now(UTC) - timedelta(minutes=age_minutes),
        ),
    )
    if body is not None:
        repo.update_article_body(aid, body)


# ---------- repo.list_articles_untranslated ----------


def test_untranslated_query_newest_first_and_filters(repo: RunHistoryRepository) -> None:
    _add(repo, "old", "body old", age_minutes=120)
    _add(repo, "new", "body new", age_minutes=1)
    _add(repo, "no-body", None, age_minutes=0)
    _add(repo, "done", "body done", age_minutes=5)
    repo.update_article_body_ja("done", "既訳")
    _add(repo, "ja-marked", "日本語の本文", age_minutes=3)
    repo.update_article_body_ja("ja-marked", "")  # 処理済・訳不要の番兵

    ids = repo.list_articles_untranslated(limit=10)

    # 新しい順、body なし / 訳済み / 訳不要番兵 ('') は除外
    assert ids == ["new", "old"]
    assert repo.list_articles_untranslated(limit=1) == ["new"]


def test_untranslated_query_dedupes_cross_run_rows(repo: RunHistoryRepository) -> None:
    # 同一 article_id は run 横断で複数行あり得る — バッチに 1 回だけ載ること
    _add(repo, "dup", "body dup", age_minutes=10)
    _add(repo, "dup", "body dup", age_minutes=1)

    assert repo.list_articles_untranslated(limit=10) == ["dup"]


# ---------- run_body_translate_backlog ----------


async def test_backlog_translates_and_caches(repo: RunHistoryRepository) -> None:
    _add(repo, "a1", "first body", age_minutes=10)
    _add(repo, "a2", "second body", age_minutes=20)

    stats = await run_body_translate_backlog(repo, FakeLLM())

    assert stats["translated"] == 2
    assert stats["failed"] == 0
    assert repo.get_article_body_ja("a1") is not None
    assert repo.get_article_body_ja("a2") is not None
    # 訳済みになったので次周期の対象はゼロ
    assert repo.list_articles_untranslated() == []


async def test_backlog_continues_after_single_failure(repo: RunHistoryRepository) -> None:
    _add(repo, "bad", "poison body", age_minutes=1)
    _add(repo, "good", "normal body", age_minutes=2)

    stats = await run_body_translate_backlog(repo, FakeLLM(fail_on={"poison"}))

    # 1 件失敗しても続行し、失敗分は未訳のまま (次周期で再試行)
    assert stats == {**stats, "translated": 1, "failed": 1}
    assert repo.get_article_body_ja("bad") is None
    assert repo.get_article_body_ja("good") is not None


async def test_backlog_respects_batch_limit(repo: RunHistoryRepository) -> None:
    for i in range(5):
        _add(repo, f"a{i}", f"body {i}", age_minutes=i)

    stats = await run_body_translate_backlog(repo, FakeLLM(), batch_limit=3)

    assert stats["picked"] == 3
    assert stats["translated"] == 3
    assert len(repo.list_articles_untranslated()) == 2


async def test_backlog_marks_japanese_body_without_llm(repo: RunHistoryRepository) -> None:
    _add(repo, "ja", "これは既に日本語で書かれた記事本文です。翻訳は不要です。", age_minutes=1)
    _add(repo, "en", "This is an English article body.", age_minutes=2)
    llm = FakeLLM()

    stats = await run_body_translate_backlog(repo, llm)

    # 日本語原文は LLM を呼ばず '' で処理済みマーク (逆方向翻訳の防止)
    assert stats["translated"] == 1
    assert stats["skipped_japanese"] == 1
    assert len(llm.calls) == 1
    assert repo.get_article_body_ja("ja") == ""
    # 次周期の対象からも外れている
    assert repo.list_articles_untranslated() == []


async def test_backlog_time_budget_stops_early(repo: RunHistoryRepository) -> None:
    for i in range(3):
        _add(repo, f"a{i}", f"body {i}", age_minutes=i)

    # 予算 0 秒 → 1 件も処理せず打切り (未処理は skipped_budget に計上)
    stats = await run_body_translate_backlog(repo, FakeLLM(), time_budget_seconds=-1)

    assert stats["translated"] == 0
    assert stats["skipped_budget"] == 3


# ---------- resumable (2026-08-06 チャンクキャッシュ) ----------


class SlowLLM(FakeLLM):
    """1 呼出 300ms の fake — チャンク境界の deadline 判定を決定的に発火させる。"""

    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        think: bool | None = None,
    ) -> LLMResponse:
        import asyncio

        await asyncio.sleep(0.3)
        return await super().generate(prompt, system, temperature, max_tokens, think)


async def test_backlog_partial_saves_progress_and_next_cycle_resumes(
    repo: RunHistoryRepository,
) -> None:
    # 4000 字 x 2 段落 = 2 チャンク。予算 0.2 秒 < 1 チャンク 0.3 秒 → 1 周期目は
    # chunk0 訳了後のチャンク境界判定で中断され partial になる
    long_body = "\n\n".join("b" * 4000 for _ in range(2))
    _add(repo, "long", long_body, age_minutes=1)
    llm = SlowLLM()

    stats1 = await run_body_translate_backlog(repo, llm, time_budget_seconds=0.2)
    assert stats1["partial"] == 1
    assert stats1["translated"] == 0
    assert repo.get_article_body_ja("long") is None  # 部分訳は body_ja に載らない

    # 2 周期目: 保存済みチャンクを再利用し、残り 1 チャンクだけ訳して完了する
    stats2 = await run_body_translate_backlog(repo, llm, time_budget_seconds=60)
    assert stats2["translated"] == 1
    assert repo.get_article_body_ja("long") is not None
    assert len(llm.calls) == 2  # 計 2 チャンク分しか LLM を呼んでいない (再訳なし)
    # 完了後はチャンク行が掃除されている
    assert repo.get_body_ja_chunks("long", "any-hash") == {}


def test_chunk_store_hash_mismatch_invalidates(repo: RunHistoryRepository) -> None:
    repo.save_body_ja_chunk("a1", 0, 2, "hash-A", "訳0")
    repo.save_body_ja_chunk("a1", 1, 2, "hash-A", "訳1")
    assert repo.get_body_ja_chunks("a1", "hash-A") == {0: "訳0", 1: "訳1"}

    # 本文差し替え (hash 変化) → 残骸を削除して空を返す
    assert repo.get_body_ja_chunks("a1", "hash-B") == {}
    assert repo.get_body_ja_chunks("a1", "hash-A") == {}


def test_chunk_store_save_is_idempotent(repo: RunHistoryRepository) -> None:
    repo.save_body_ja_chunk("a1", 0, 1, "h", "最初の訳")
    repo.save_body_ja_chunk("a1", 0, 1, "h", "上書きしようとした訳")
    assert repo.get_body_ja_chunks("a1", "h") == {0: "最初の訳"}
    assert repo.clear_body_ja_chunks("a1") == 1
