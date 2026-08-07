"""Phase 2: 検索の全文化 (K3) + 逆引き pivot の repo 結合 (K2)。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "p2.db")


def _add(repo: RunHistoryRepository, aid: str, title: str = "t", body: str | None = None) -> None:
    rid = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(run_id=rid, article_id=aid, title=title, url=f"u-{aid}", status="posted"),
    )
    if body is not None:
        repo.update_article_body(aid, body)


# ---------- K3: 全文検索 (body まで) ----------


@pytest.mark.unit
def test_search_matches_body_not_just_title(repo: RunHistoryRepository) -> None:
    _add(repo, "a1", title="generic headline", body="the unique token zxqq lives only in body")
    # title/summary に無い語が body 検索で当たる
    found = repo.list_articles(search="zxqq", limit=10)
    assert any(a.article_id == "a1" for a in found)
    # 無関係語は当たらない
    assert repo.list_articles(search="nonexistent_qqq", limit=10) == []


@pytest.mark.unit
def test_search_still_matches_title(repo: RunHistoryRepository) -> None:
    _add(repo, "a2", title="Volt Typhoon prepositioning", body="body text")
    found = repo.list_articles(search="volt typhoon", limit=10)
    assert any(a.article_id == "a2" for a in found)


# ---------- K2: 逆引き pivot (find_articles_by_entity + count_entities_for_articles) ----------


@pytest.mark.unit
def test_reverse_pivot_cooccurrence(repo: RunHistoryRepository) -> None:
    _add(repo, "a1")
    _add(repo, "a2")
    repo.add_article_entities(
        "a1",
        [("ioc_ip", "1.2.3.4"), ("actor", "lazarus"), ("cve", "CVE-2026-1")],
    )
    repo.add_article_entities(
        "a2", [("ioc_ip", "1.2.3.4"), ("actor", "lazarus"), ("actor", "apt41")]
    )

    # ioc_ip を起点に参照記事を逆引き
    ids = repo.find_articles_by_entity("ioc_ip", "1.2.3.4")
    assert set(ids) == {"a1", "a2"}

    # 共起 entity の集計 (actor 帰属 / 相関 CVE)
    co = repo.count_entities_for_articles(ids)
    assert co["actor"]["lazarus"] == 2
    assert co["actor"]["apt41"] == 1
    assert co["cve"]["CVE-2026-1"] == 1
    assert co["ioc_ip"]["1.2.3.4"] == 2  # query 自身 (API 層で除外する)
