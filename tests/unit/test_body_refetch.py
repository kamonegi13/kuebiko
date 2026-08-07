"""切り株再取得キュー + entity replace + reprocess の単体テスト (2026-07-27, A4)。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.storage.records import ArticleRecord, RunRecord
from src.storage.run_history import RunHistoryRepository
from src.tools.content_extractor import ExtractionResult


def _repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=str(tmp_path / "refetch.db"))


def _add(repo: RunHistoryRepository, aid: str, **kw: object) -> None:
    rid = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=rid,
            article_id=aid,
            title=str(kw.get("title", "t")),
            url=str(kw.get("url", f"https://ex.com/{aid}")),
            feed_title=kw.get("feed_title"),  # type: ignore[arg-type]
            status="posted",
        )
    )


def test_refetch_queue_targets_stumps_and_nulls(tmp_path: Path) -> None:
    """再取得対象 = feed_summary(retriable失敗) と body NULL。full_extract は対象外。"""
    repo = _repo(tmp_path)
    _add(repo, "full")
    repo.update_article_body("full", "x" * 3000, source="full_extract")
    _add(repo, "stump")
    repo.update_article_body("stump", "short", source="feed_summary", failure_reason="http_403")
    _add(repo, "paywall")  # 恒久失敗は対象外
    repo.update_article_body(
        "paywall", "short", source="feed_summary", failure_reason="paywall_suspected"
    )
    _add(repo, "predeploy")  # UA 修正前の既存切り株 (heuristic 分類・failure_reason NULL) → 対象
    repo.update_article_body("predeploy", "short feed", source="feed_summary")
    _add(repo, "nobody")  # body NULL → 対象

    _add(repo, "grok_null", feed_title="Grok")  # body NULL だが URL 直取得しない source
    _add(repo, "ransom_null", feed_title="Ransomware.live")

    targets = {aid for aid, _ in repo.list_articles_needing_refetch(limit=50)}
    assert "stump" in targets
    assert "nobody" in targets
    assert "predeploy" in targets  # 既存切り株 (failure_reason NULL) も遡及対象
    assert "full" not in targets
    assert "paywall" not in targets  # 恒久失敗は除外
    # grok/ransomware.live は URL 直取得しないため再取得キューから除外
    assert "grok_null" not in targets
    assert "ransom_null" not in targets


def test_delete_article_entities_replaces(tmp_path: Path) -> None:
    """delete_article_entities は指定 type のみ削除する (entity replace の土台)。"""
    repo = _repo(tmp_path)
    _add(repo, "a1")
    repo.add_article_entities("a1", [("actor", "us_nsa"), ("cve", "CVE-2026-1"), ("pir", "p1")])
    deleted = repo.delete_article_entities("a1", ["actor", "cve"])
    assert deleted == 2
    remaining = dict(repo.get_entities_by_article("a1"))
    assert "us_nsa" not in remaining.values()
    assert ("pir", "p1") in repo.get_entities_by_article("a1")


def test_update_article_enrichment_allowlist(tmp_path: Path) -> None:
    """update_article_enrichment は allowlist 列のみ更新し、未知キーは無視する。"""
    repo = _repo(tmp_path)
    _add(repo, "a1")
    repo.update_article_enrichment(
        "a1",
        {"summary": "new summary", "article_type": "advisory", "evil_col": "x", "pmesii_e": 1},
    )
    rec = repo.get_article("a1")
    assert rec is not None
    assert rec.summary == "new summary"
    assert rec.article_type == "advisory"
    assert rec.pmesii_e is True


def test_clear_body_ja(tmp_path: Path) -> None:
    """clear_article_body_ja は body_ja を NULL に戻す (再翻訳キュー投入)。"""
    repo = _repo(tmp_path)
    _add(repo, "a1")
    repo.update_article_body("a1", "body", source="full_extract")
    repo.update_article_body_ja("a1", "訳文")
    assert repo.get_article_body_ja("a1") == "訳文"
    repo.clear_article_body_ja("a1")
    assert repo.get_article_body_ja("a1") is None


@pytest.mark.asyncio
async def test_reprocess_refetch_failure_leaves_stump(tmp_path: Path) -> None:
    """再取得も失敗したら body を触らず 'still_stump' を返す (無害)。"""
    from src.pipeline.reprocess import reprocess_article_body

    repo = _repo(tmp_path)
    url = "https://ex.com/stump"
    _add(repo, "stump", url=url)
    repo.update_article_body(
        "stump", "short feed", source="feed_summary", failure_reason="http_403"
    )

    extractor = AsyncMock()
    extractor.extract.return_value = ExtractionResult(
        url=url, success=False, failure_reason="http_error_403"
    )
    outcome = await reprocess_article_body(
        repo, "stump", url, extractor=extractor, llm=AsyncMock(), template=object()
    )
    assert outcome == "still_stump"
    # body は切り株のまま (触っていない)
    assert repo.get_article_body("stump") == "short feed"
    rec = repo.get_article("stump")
    assert rec is not None and rec.body_source == "feed_summary"
