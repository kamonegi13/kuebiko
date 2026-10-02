"""まとめ記事は重複判定の対象にしない (2026-10-02)。

規則の重複判定 (CVE / 内容 / 被害組織) は、まとめ記事を中の 1 話題の記事の重複として
落としていた (「ThreatsDay … 13 本」が Zammad の 1 本の重複に)。逆に、まとめ記事を先に
取り込むと、その中の話題を扱う個別記事が重複にされる。どちらの向きも重複ではない。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.pipeline.roundup_guard import involves_roundup
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    r = RunHistoryRepository(db_path=tmp_path / "rg.db")
    rid = r.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
    r.add_article(
        ArticleRecord(
            run_id=rid,
            article_id="prior",
            title="週次まとめ: 13 本",
            url="https://kuebiko.example/prior",
            status="posted",
            summary="複数の話題",
            created_at=datetime.now(UTC),
        )
    )
    return r


def _classifier(answers: dict[str, str], calls: list[str]):  # type: ignore[no-untyped-def]
    async def classify(title: str, _summary: str) -> str:
        calls.append(title)
        return answers.get(title, "other")

    return classify


@pytest.mark.asyncio
async def test_current_roundup_is_exempt(repo: RunHistoryRepository) -> None:
    calls: list[str] = []
    hit = await involves_roundup(
        repo,
        _classifier({"今週のまとめ": "roundup"}, calls),
        article_id="cur",
        title="今週のまとめ",
        summary="",
        prior_article_id=None,
    )

    assert hit is True
    assert repo.get_article_kinds(["cur"]) == {"cur": "roundup"}  # 分類はキャッシュする


@pytest.mark.asyncio
async def test_prior_roundup_is_exempt_and_classified_from_db(
    repo: RunHistoryRepository,
) -> None:
    calls: list[str] = []
    hit = await involves_roundup(
        repo,
        _classifier({"週次まとめ: 13 本": "roundup"}, calls),
        article_id="cur",
        title="Zammad の 0day",
        summary="",
        prior_article_id="prior",
    )

    assert hit is True
    assert "週次まとめ: 13 本" in calls


@pytest.mark.asyncio
async def test_cached_kinds_are_not_reclassified(repo: RunHistoryRepository) -> None:
    repo.set_article_kind("cur", "breach", "m")
    repo.set_article_kind("prior", "breach", "m")
    calls: list[str] = []

    hit = await involves_roundup(
        repo,
        _classifier({}, calls),
        article_id="cur",
        title="x",
        summary="",
        prior_article_id="prior",
    )

    assert hit is False
    assert calls == []


@pytest.mark.asyncio
async def test_classifier_failure_keeps_dedup(repo: RunHistoryRepository) -> None:
    """分類に失敗したら従来どおり重複として扱う (判定できないものを免除しない)。"""

    async def broken(_t: str, _s: str) -> str:
        raise RuntimeError("down")

    hit = await involves_roundup(
        repo, broken, article_id="cur", title="x", summary="", prior_article_id="prior"
    )

    assert hit is False
