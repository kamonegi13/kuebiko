"""再取得で本文が取れた extract_failed の記事を分析に戻す (2026-11-20、11-19 監査 D2 P0-3a)。

- 取り直した本文にも取り込みと同じ決定論の関門を当てる (``REPROCESS_BODY_GATE``)。
  当てずに要約していたため、薄い本文から要約・entity が作られた行が 14 日で 82 件あった。
- 関門を通った本文で要約できたら、状態を posted・分析専用 (``late_body``) に進める
  (``LATE_BODY_PROMOTE``)。配信はしない。従来は要約まで書いて status が進まず、
  191 件が分析から外れていた (Global Times は 24 件中 24 件)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.storage.records import ArticleRecord, RunRecord
from src.storage.run_history import RunHistoryRepository
from src.tools.content_extractor import ExtractionResult

LONG_BODY = "本文の段落です。攻撃者は複数の組織に侵入した。" * 40


def _repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=str(tmp_path / "late.db"))


def _add_failed(repo: RunHistoryRepository, aid: str, reason: str) -> None:
    rid = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=rid,
            article_id=aid,
            title="t",
            url=f"https://ex.com/{aid}",
            status="extract_failed",
            failure_reason=reason,
        )
    )


def _extractor(text: str) -> AsyncMock:
    ex = AsyncMock()
    ex.extract.return_value = ExtractionResult(url="u", success=True, text=text)
    return ex


@pytest.fixture
def _stub_llm(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    from src.pipeline import reprocess

    summarize = AsyncMock(
        return_value=SimpleNamespace(importance="medium", category="geopolitical")
    )
    monkeypatch.setattr(reprocess, "_summarize_and_build", summarize)
    monkeypatch.setattr(reprocess, "reenrich_stored_article", lambda *a, **k: None)
    return summarize


async def _run(repo: RunHistoryRepository, aid: str, text: str) -> str:
    import jinja2

    from src.pipeline.reprocess import reprocess_article_body

    return await reprocess_article_body(
        repo,
        aid,
        f"https://ex.com/{aid}",
        extractor=_extractor(text),
        llm=AsyncMock(),
        template=jinja2.Template("x"),
    )


@pytest.mark.asyncio
async def test_gate_on_skips_summarizing_a_degenerate_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stub_llm: AsyncMock
) -> None:
    monkeypatch.setenv("REPROCESS_BODY_GATE", "1")
    repo = _repo(tmp_path)
    _add_failed(repo, "a", "degenerate_body: body_too_short:0")
    assert await _run(repo, "a", "短い") == "still_degenerate"
    _stub_llm.assert_not_called()


@pytest.mark.asyncio
async def test_gate_shadow_keeps_old_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stub_llm: AsyncMock
) -> None:
    monkeypatch.setenv("REPROCESS_BODY_GATE", "shadow")
    monkeypatch.setenv("LATE_BODY_PROMOTE", "0")
    repo = _repo(tmp_path)
    _add_failed(repo, "a", "degenerate_body: body_too_short:0")
    assert await _run(repo, "a", "短い") == "refetched_full"
    _stub_llm.assert_called_once()


@pytest.mark.asyncio
async def test_promote_moves_recovered_row_to_analysis_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stub_llm: AsyncMock
) -> None:
    monkeypatch.setenv("LATE_BODY_PROMOTE", "1")
    repo = _repo(tmp_path)
    _add_failed(repo, "a", "degenerate_body: body_too_short:0")
    assert await _run(repo, "a", LONG_BODY) == "refetched_full"
    rec = repo.get_article("a")
    assert rec is not None
    assert rec.status == "posted"
    assert rec.analysis_only == "late_body"
    assert rec.posted_channel is None
    assert rec.importance == "medium"


@pytest.mark.asyncio
async def test_promote_off_leaves_extract_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stub_llm: AsyncMock
) -> None:
    monkeypatch.setenv("LATE_BODY_PROMOTE", "0")
    repo = _repo(tmp_path)
    _add_failed(repo, "a", "degenerate_body: body_too_short:0")
    await _run(repo, "a", LONG_BODY)
    rec = repo.get_article("a")
    assert rec is not None and rec.status == "extract_failed"


@pytest.mark.asyncio
async def test_promote_never_promotes_a_still_degenerate_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stub_llm: AsyncMock
) -> None:
    # 関門が shadow でも、昇格は関門を通った本文だけ
    monkeypatch.setenv("REPROCESS_BODY_GATE", "shadow")
    monkeypatch.setenv("LATE_BODY_PROMOTE", "1")
    repo = _repo(tmp_path)
    _add_failed(repo, "a", "degenerate_body: body_too_short:0")
    await _run(repo, "a", "短い")
    rec = repo.get_article("a")
    assert rec is not None and rec.status == "extract_failed"


def test_promote_is_idempotent(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _add_failed(repo, "a", "degenerate_body: thin_body")
    first = repo.promote_late_body("a", importance="low", category="other")
    second = repo.promote_late_body("a", importance="low", category="other")
    assert (first, second) == (1, 0)


def test_promote_skips_when_a_posted_row_exists(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _add_failed(repo, "a", "degenerate_body: thin_body")
    rid = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=rid, article_id="a", title="t", url="https://ex.com/a", status="posted"
        )
    )
    assert repo.promote_late_body("a", importance="low", category="other") == 0


# ---- 既存の行の回収 (scripts/recover_thin_body_articles.py --target late_body) ----


def _add_failed_with_body(repo: RunHistoryRepository, aid: str, reason: str, body: str) -> None:
    _add_failed(repo, aid, reason)
    repo.update_article_body(aid, body, source="full_extract")


def test_list_degenerate_candidates_excludes_articles_already_posted(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _add_failed_with_body(repo, "a", "degenerate_body: body_too_short:0", LONG_BODY)
    _add_failed_with_body(repo, "b", "degenerate_body: block_page", LONG_BODY)
    _add_failed_with_body(repo, "c", "http_error_403", LONG_BODY)
    rid = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=rid, article_id="b", title="t", url="https://ex.com/b", status="posted"
        )
    )
    ids = {str(r["article_id"]) for r in repo.list_degenerate_extract_failed()}
    assert ids == {"a"}


@pytest.mark.asyncio
async def test_recover_one_late_body_target_promotes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.pipeline import thin_body_recovery

    repo = _repo(tmp_path)
    _add_failed_with_body(repo, "a", "degenerate_body: body_too_short:0", LONG_BODY)
    reply = SimpleNamespace(importance="low", category="geopolitical")
    monkeypatch.setattr(thin_body_recovery, "_summarize_and_build", AsyncMock(return_value=reply))
    monkeypatch.setattr(thin_body_recovery, "reenrich_stored_article", lambda *a, **k: None)
    row = repo.list_degenerate_extract_failed()[0]
    import jinja2

    done = await thin_body_recovery.recover_one(
        repo,
        row,
        llm=AsyncMock(),
        template=jinja2.Template("x"),
        enrichment=None,
        target="late_body",
    )
    rec = repo.get_article("a")
    assert done is True
    assert rec is not None and rec.analysis_only == "late_body" and rec.status == "posted"


def test_sql_has_no_bare_percent_for_pg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PG (psycopg) は % をプレースホルダーとみなす — LIKE の % は直書きせず値で渡す。

    2026-11-20: SQLite のテストでは通り、本番 PG で ProgrammingError になった。
    """
    import contextlib
    import re
    from collections.abc import Iterator
    from typing import Any

    from src.storage.db_backend import translate_sql

    sent: list[str] = []

    class _Cur:
        rowcount = 0

        def fetchone(self) -> None:
            return None

        def fetchall(self) -> list[Any]:
            return []

    class _Conn:
        def execute(self, sql: str, params: Any = ()) -> _Cur:
            sent.append(translate_sql(sql))
            return _Cur()

    repo = _repo(tmp_path)

    @contextlib.contextmanager
    def _fake_connect() -> Iterator[_Conn]:
        yield _Conn()

    monkeypatch.setattr(repo, "_connect", _fake_connect)
    repo.list_degenerate_extract_failed()
    repo.promote_late_body("a", importance="low", category="other")
    assert sent
    for sql in sent:
        assert not re.search(r"%(?![%sbt])", sql), sql
