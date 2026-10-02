"""群化の入力に記事の種別を付ける (まとめ記事を群化に参加させない、2026-10-02)。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.eventnews.models import MemberArticle
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord
from src.ui.services import eventnews_hourly_job as job


def _member(aid: str) -> MemberArticle:
    return MemberArticle(
        article_id=aid,
        title=f"t-{aid}",
        url=f"https://kuebiko.example/{aid}",
        feed_title="f",
        feed_url="",
        host="kuebiko.example",
        importance="medium",
        category="incident",
        status="posted",
        anchor_ts=datetime.now(UTC),
        summary="s",
        body="b",
        entities=frozenset(),
    )


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "k.db")


@pytest.mark.asyncio
async def test_resolve_kinds_classifies_up_to_limit_and_leaves_rest(
    repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: 1 件はキャッシュ済み、未分類 3 件のうち上限 2 件だけ分類する
    repo.set_article_kind("cached", "roundup", "m")
    calls: list[str] = []

    async def fake_classify(_llm: Any, title: str, _summary: str) -> str:
        calls.append(title)
        return "breach"

    monkeypatch.setattr(job, "build_llm_for", lambda *_a, **_k: object())
    monkeypatch.setattr("src.eventnews.event_kind.classify", fake_classify)
    arts = [_member(a) for a in ("cached", "a", "b", "c")]

    # Act
    kinds = await job._resolve_kinds(repo, object(), arts, limit=2)  # type: ignore[arg-type]

    # Assert
    assert kinds == {"cached": "roundup", "a": "breach", "b": "breach"}
    assert calls == ["t-a", "t-b"]
    assert repo.get_article_kinds(["a", "b", "c"]) == {"a": "breach", "b": "breach"}


def test_load_members_attaches_cached_kind(repo: RunHistoryRepository) -> None:
    rid = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
    for aid in ("r1", "n1"):
        repo.add_article(
            ArticleRecord(
                run_id=rid,
                article_id=aid,
                title="t",
                url=f"https://kuebiko.example/{aid}",
                status="posted",
                created_at=datetime.now(UTC),
            )
        )
    repo.set_article_kind("r1", "roundup", "m")

    members = job._load_members(repo, ["r1", "n1"], {})

    assert members["r1"].is_roundup
    assert not members["n1"].is_roundup
    assert members["n1"].kind == ""
