"""actor-history-distill ジョブ (F7 蒸留 service) のテスト。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.storage.records import ArticleRecord, RunRecord
from src.storage.run_history import RunHistoryRepository
from src.ui.services.actor_history_distill import (
    distill_and_store,
    months_to_distill,
    run_actor_history_distill,
)


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "distill.db")


def _add_article(
    repo: RunHistoryRepository,
    aid: str,
    *,
    subject: str,
    created_at: datetime,
    feed_url: str = "https://feed.example/a",
    victim_country: str | None = None,
) -> None:
    rid = repo.start_run(RunRecord(started_at=created_at, pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=rid,
            article_id=aid,
            title=f"t-{aid}",
            url=f"https://x.example/{aid}",
            status="posted",
            subject_actor_ids=subject,
            subject_actor_source="title",
            feed_url=feed_url,
            victim_country_iso=victim_country,
            created_at=created_at,
        ),
    )


# ---------- months_to_distill ----------


def test_months_backfill_from_epoch_when_table_empty(repo: RunHistoryRepository) -> None:
    # epoch = 2026-04 (収集開始月。title 層は全史遡及可能 — D3+ で拡張)
    now = datetime(2026, 9, 15, tzinfo=UTC)
    assert months_to_distill(repo, now=now) == [
        "2026-04",
        "2026-05",
        "2026-06",
        "2026-07",
        "2026-08",
        "2026-09",
    ]


def test_months_regular_run_covers_current_and_previous(repo: RunHistoryRepository) -> None:
    # 1 行でも存在すれば定常モード = 当月+前月 (月末尾の取りこぼし回収)
    distill_and_store(repo, ["2026-07"], kev_cves=frozenset())
    _add_article(repo, "seed", subject="qilin", created_at=datetime(2026, 7, 20, tzinfo=UTC))
    distill_and_store(repo, ["2026-07"], kev_cves=frozenset())
    now = datetime(2026, 9, 15, tzinfo=UTC)
    assert months_to_distill(repo, now=now) == ["2026-08", "2026-09"]


def test_months_epoch_month_has_no_previous(repo: RunHistoryRepository) -> None:
    _add_article(repo, "a", subject="qilin", created_at=datetime(2026, 4, 29, tzinfo=UTC))
    distill_and_store(repo, ["2026-04"], kev_cves=frozenset())
    now = datetime(2026, 4, 30, tzinfo=UTC)
    assert months_to_distill(repo, now=now) == ["2026-04"]


# ---------- distill_and_store (end-to-end) ----------


def test_distill_and_store_writes_month_profiles(repo: RunHistoryRepository) -> None:
    _add_article(
        repo,
        "a1",
        subject="qilin",
        created_at=datetime(2026, 7, 10, tzinfo=UTC),
        feed_url="https://f1",
        victim_country="JP",
    )
    _add_article(
        repo,
        "a2",
        subject="qilin,apt29",
        created_at=datetime(2026, 7, 12, tzinfo=UTC),
        feed_url="https://f2",
    )
    repo.add_article_entities("a1", [("cve", "CVE-2026-9999"), ("malware_family", "Qilin")])
    stats = distill_and_store(repo, ["2026-07"], kev_cves=frozenset({"CVE-2026-9999"}))
    assert stats == {"months": 1, "profiles": 2, "articles": 2}
    q = repo.list_actor_month_profiles(["qilin"])[0]
    assert q.subject_articles == 2
    assert q.distinct_sources == 2
    assert q.japan_targeted == 1
    assert q.kev_hits == 1
    assert q.malware == {"Qilin": 1}
    a = repo.list_actor_month_profiles(["apt29"])[0]
    assert a.subject_articles == 1


def test_distill_respects_jst_month_boundary(repo: RunHistoryRepository) -> None:
    # UTC 7/31 16:00 = JST 8/1 01:00 → 2026-08 の行に入る (2026-07 には入らない)
    _add_article(repo, "a1", subject="qilin", created_at=datetime(2026, 7, 31, 16, 0, tzinfo=UTC))
    distill_and_store(repo, ["2026-07", "2026-08"], kev_cves=frozenset())
    months = [p.month for p in repo.list_actor_month_profiles(["qilin"])]
    assert months == ["2026-08"]


async def test_run_job_backfills_on_first_run(
    repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.ui.services.actor_history_distill._kev_cve_set", lambda: frozenset())
    _add_article(repo, "a1", subject="qilin", created_at=datetime(2026, 7, 20, tzinfo=UTC))
    stats = await run_actor_history_distill(repo)
    assert stats["months"] >= 1
    assert repo.count_actor_profile_rows() >= 1


async def test_job_sweeps_unevaluated_titles_before_distill(
    repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """briefing 永続化を経ない取込 (ransomware.live 等) の subject 未評価を週次で回収する。"""
    from src.cti.actor_normalizer import ActorAlias, ActorAliasRegistry

    monkeypatch.setattr("src.ui.services.actor_history_distill._kev_cve_set", lambda: frozenset())
    reg = ActorAliasRegistry(actors=(ActorAlias(id="qilin", canonical="Qilin"),))
    monkeypatch.setattr("src.cti.actor_normalizer.load_actor_aliases", lambda *a, **k: reg)
    created = datetime.now(UTC)
    rid = repo.start_run(RunRecord(started_at=created, pipeline="x", dry_run=False))
    # subject 未評価 (source NULL) の collected 記事 — title にアクター名
    repo.add_article(
        ArticleRecord(
            run_id=rid,
            article_id="rl-1",
            title="Qilin、新たな被害組織として example.com を公開",
            url="https://x.example/rl-1",
            status="collected",
            created_at=created,
        ),
    )
    stats = await run_actor_history_distill(repo)
    assert stats["swept_titles"] == 1
    profiles = repo.list_actor_month_profiles(["qilin"])
    assert sum(p.subject_articles for p in profiles) == 1
