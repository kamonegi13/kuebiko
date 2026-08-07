"""src.digest.db_filter.fetch_for_deep_dive_candidates のテスト (Phase 5T-T1)。

Stage 0 (機械 prefilter) + Stage 1 (dedup_key cluster) の動作を検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.digest.db_filter import (
    DeepDivePrefilterResult,
    fetch_for_deep_dive_candidates,
    fetch_recent_brief_titles,
)
from src.storage.run_history import (
    ArticleRecord,
    RunHistoryRepository,
    RunRecord,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "deep_dive.db"


def _now() -> datetime:
    return datetime.now(UTC)


def _seed_article(
    repo: RunHistoryRepository,
    run_id: int,
    *,
    article_id: str,
    importance: str | None = "medium",
    posted_channel: str | None = "watch",
    summary: str | None = "x" * 300,
    dedup_key: str | None = None,
    created_at: datetime | None = None,
    title: str | None = None,
) -> None:
    """テスト用 article をシード投入する。"""
    repo.add_article(
        ArticleRecord(
            run_id=run_id,
            article_id=article_id,
            title=title or f"Title {article_id}",
            url=f"https://example.com/{article_id}",
            feed_title="Test Feed",
            importance=importance,
            category="apt",
            status="posted",
            posted_channel=posted_channel,
            summary=summary,
            dedup_key=dedup_key,
            created_at=created_at or _now(),
        ),
    )


def _make_repo(db_path: Path) -> tuple[RunHistoryRepository, int]:
    repo = RunHistoryRepository(db_path=db_path)
    run_id = repo.start_run(
        RunRecord(started_at=_now(), pipeline="web-scraper-watchers", dry_run=False),
    )
    return repo, run_id


class TestStageZeroBasics:
    """Stage 0: 機械 prefilter の基本 (channel/window/importance/summary)。"""

    def test_excludes_non_watch_channel(self, db_path: Path) -> None:
        repo, run_id = _make_repo(db_path)
        _seed_article(repo, run_id, article_id="A1", posted_channel="brief")
        _seed_article(repo, run_id, article_id="A2", posted_channel="watch")
        result = fetch_for_deep_dive_candidates(db_path=db_path)
        assert isinstance(result, DeepDivePrefilterResult)
        ids = {c.article_id for c in result.candidates}
        assert ids == {"A2"}

    def test_excludes_outside_lookback(self, db_path: Path) -> None:
        repo, run_id = _make_repo(db_path)
        old = _now() - timedelta(hours=200)
        _seed_article(repo, run_id, article_id="OLD", created_at=old)
        _seed_article(repo, run_id, article_id="NEW")
        result = fetch_for_deep_dive_candidates(
            lookback_hours=168,
            db_path=db_path,
        )
        ids = {c.article_id for c in result.candidates}
        assert ids == {"NEW"}

    def test_excludes_low_importance(self, db_path: Path) -> None:
        repo, run_id = _make_repo(db_path)
        _seed_article(repo, run_id, article_id="LOW", importance="low")
        _seed_article(repo, run_id, article_id="MED", importance="medium")
        _seed_article(repo, run_id, article_id="HI", importance="high")
        _seed_article(repo, run_id, article_id="NONE", importance=None)
        result = fetch_for_deep_dive_candidates(db_path=db_path)
        ids = {c.article_id for c in result.candidates}
        assert ids == {"MED", "HI"}

    def test_excludes_short_summary(self, db_path: Path) -> None:
        repo, run_id = _make_repo(db_path)
        _seed_article(repo, run_id, article_id="SHORT", summary="too short")
        _seed_article(repo, run_id, article_id="NULL", summary=None)
        _seed_article(repo, run_id, article_id="LONG", summary="x" * 300)
        result = fetch_for_deep_dive_candidates(
            min_summary_chars=150,
            db_path=db_path,
        )
        ids = {c.article_id for c in result.candidates}
        assert ids == {"LONG"}


class TestNoveltyExclusion:
    """Stage 0: 過去 F1 選定済 dedup_key の除外。"""

    def test_excludes_previously_selected_dedup_keys(self, db_path: Path) -> None:
        repo, run_id = _make_repo(db_path)
        _seed_article(
            repo,
            run_id,
            article_id="A1",
            dedup_key="apt41-japan",
        )
        _seed_article(
            repo,
            run_id,
            article_id="A2",
            dedup_key="new-incident",
        )
        result = fetch_for_deep_dive_candidates(
            db_path=db_path,
            novelty_excluded_dedup_keys={"apt41-japan"},
        )
        ids = {c.article_id for c in result.candidates}
        assert ids == {"A2"}

    def test_null_dedup_key_is_always_kept(self, db_path: Path) -> None:
        repo, run_id = _make_repo(db_path)
        _seed_article(repo, run_id, article_id="NULL_KEY", dedup_key=None)
        result = fetch_for_deep_dive_candidates(
            db_path=db_path,
            novelty_excluded_dedup_keys={"some-key"},
        )
        ids = {c.article_id for c in result.candidates}
        assert ids == {"NULL_KEY"}


class TestStageOneCluster:
    """Stage 1: dedup_key cluster で代表 1 件のみ残す。"""

    def test_dedup_key_cluster_keeps_latest_only(self, db_path: Path) -> None:
        repo, run_id = _make_repo(db_path)
        older = _now() - timedelta(hours=10)
        newer = _now() - timedelta(hours=1)
        _seed_article(
            repo,
            run_id,
            article_id="OLDER",
            dedup_key="same-incident",
            created_at=older,
        )
        _seed_article(
            repo,
            run_id,
            article_id="NEWER",
            dedup_key="same-incident",
            created_at=newer,
        )
        result = fetch_for_deep_dive_candidates(db_path=db_path)
        ids = [c.article_id for c in result.candidates]
        assert ids == ["NEWER"]

    def test_null_dedup_keys_are_all_kept(self, db_path: Path) -> None:
        repo, run_id = _make_repo(db_path)
        _seed_article(repo, run_id, article_id="N1", dedup_key=None)
        _seed_article(repo, run_id, article_id="N2", dedup_key=None)
        result = fetch_for_deep_dive_candidates(db_path=db_path)
        ids = {c.article_id for c in result.candidates}
        assert ids == {"N1", "N2"}


class TestRecentBriefTitles:
    def test_picks_only_brief_and_alert(self, db_path: Path) -> None:
        repo, run_id = _make_repo(db_path)
        _seed_article(repo, run_id, article_id="B1", posted_channel="brief", title="Brief X")
        _seed_article(repo, run_id, article_id="A1", posted_channel="alert", title="Alert Y")
        _seed_article(repo, run_id, article_id="W1", posted_channel="watch", title="Watch Z")
        titles = fetch_recent_brief_titles(db_path=db_path)
        assert "Brief X" in titles
        assert "Alert Y" in titles
        assert "Watch Z" not in titles

    def test_filters_by_lookback(self, db_path: Path) -> None:
        repo, run_id = _make_repo(db_path)
        old = _now() - timedelta(hours=200)
        _seed_article(
            repo,
            run_id,
            article_id="OLD",
            posted_channel="brief",
            title="Old Brief",
            created_at=old,
        )
        _seed_article(repo, run_id, article_id="NEW", posted_channel="brief", title="New Brief")
        titles = fetch_recent_brief_titles(lookback_hours=168, db_path=db_path)
        assert "New Brief" in titles
        assert "Old Brief" not in titles


class TestStageCounts:
    """戻り値の stage_counts (selection stage log 用) を検証。"""

    def test_stage_counts_track_each_stage(self, db_path: Path) -> None:
        repo, run_id = _make_repo(db_path)
        # total_watch: 5
        _seed_article(repo, run_id, article_id="W1", importance="medium")
        _seed_article(repo, run_id, article_id="W2", importance="medium")
        _seed_article(repo, run_id, article_id="W3", importance="low")  # importance fail
        _seed_article(
            repo,
            run_id,
            article_id="W4",
            importance="medium",
            summary="x",  # summary fail
        )
        _seed_article(
            repo,
            run_id,
            article_id="W5",
            importance="medium",
            dedup_key="dup",
        )
        _seed_article(
            repo,
            run_id,
            article_id="W6",
            importance="medium",
            dedup_key="dup",  # cluster fail (older sibling kept depending on order)
            created_at=_now() - timedelta(hours=2),
        )
        result = fetch_for_deep_dive_candidates(
            min_summary_chars=150,
            db_path=db_path,
        )
        sc = result.stage_counts
        assert sc["total_watch"] == 6
        assert sc["after_importance"] == 5  # W3 落ちる
        assert sc["after_summary"] == 4  # W4 落ちる
        assert sc["after_novelty"] == 4  # novelty 除外なし
        assert sc["after_cluster"] == 3  # W5/W6 で 1 件に


class TestStageTwoChunkAll:
    """Stage 2 (2026-07-07, chunk-all): prefilter は全件返し、significance 降順に整列。

    selector が bounded なバッチに分けて全件採点するため、prefilter では絞らない。
    safety_max は暴走防止の runaway guard のみ (通常週は効かない)。
    """

    def test_returns_all_candidates_no_cap(self, db_path: Path) -> None:
        repo, run_id = _make_repo(db_path)
        for i in range(20):
            _seed_article(repo, run_id, article_id=f"A{i:02d}")
        result = fetch_for_deep_dive_candidates(db_path=db_path)
        assert len(result.candidates) == 20  # 全件 (絞らない)
        assert result.stage_counts["after_cluster"] == 20
        assert result.stage_counts["after_cap"] == 20

    def test_ordered_high_first_then_significance(self, db_path: Path) -> None:
        repo, run_id = _make_repo(db_path)
        base = _now()
        _seed_article(
            repo,
            run_id,
            article_id="MED",
            importance="medium",
            created_at=base - timedelta(hours=1),
        )
        _seed_article(
            repo, run_id, article_id="HI", importance="high", created_at=base - timedelta(hours=50)
        )
        result = fetch_for_deep_dive_candidates(db_path=db_path)
        # 新しい medium より、古くても high が先頭 (significance 降順)
        assert result.candidates[0].article_id == "HI"

    def test_corroboration_orders_within_high(self, db_path: Path) -> None:
        repo, run_id = _make_repo(db_path)
        base = _now()
        for i in range(4):  # Story A: 裏取り 4、やや古い
            _seed_article(
                repo,
                run_id,
                article_id=f"A{i}",
                importance="high",
                dedup_key="storyA",
                created_at=base - timedelta(hours=20 + i),
            )
        _seed_article(  # Story B: 単発、最新
            repo,
            run_id,
            article_id="B",
            importance="high",
            dedup_key="storyB",
            created_at=base - timedelta(hours=1),
        )
        result = fetch_for_deep_dive_candidates(db_path=db_path)
        # 先頭は「新しい B」でなく「裏取りの多い Story A」の代表
        assert result.candidates[0].dedup_key == "storyA"

    def test_safety_max_only_caps_in_runaway(self, db_path: Path) -> None:
        repo, run_id = _make_repo(db_path)
        for i in range(10):
            _seed_article(repo, run_id, article_id=f"A{i:02d}", importance="high")
        # safety_max=3 で暴走時のみ cap (significance 上位を残す)
        result = fetch_for_deep_dive_candidates(db_path=db_path, safety_max=3)
        assert len(result.candidates) == 3
        assert result.stage_counts["after_cluster"] == 10
        assert result.stage_counts["after_cap"] == 3


class TestDeterministicComposite:
    """Stage 2' (2026-07-20): 週中に蓄積したシグナルの決定論射影による有界 shortlist。"""

    def test_pool_capped_at_rubric_pool_max(self, db_path: Path) -> None:
        from src.digest.db_filter import RUBRIC_POOL_MAX

        repo = RunHistoryRepository(db_path=db_path)
        run_id = repo.start_run(RunRecord(started_at=_now(), pipeline="t", dry_run=False))
        for i in range(RUBRIC_POOL_MAX + 30):
            _seed_article(repo, run_id, article_id=f"m{i}", importance="medium")
        result = fetch_for_deep_dive_candidates(db_path=db_path)
        assert len(result.candidates) == RUBRIC_POOL_MAX

    def test_high_and_pir_and_kev_outrank_plain_medium(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.digest import db_filter as mod
        from src.digest.db_filter import RUBRIC_POOL_MAX

        repo = RunHistoryRepository(db_path=db_path)
        run_id = repo.start_run(RunRecord(started_at=_now(), pipeline="t", dry_run=False))
        # 無印 medium を上限まで埋める
        for i in range(RUBRIC_POOL_MAX):
            _seed_article(repo, run_id, article_id=f"m{i}", importance="medium")
        # シグナル付き候補 (PIR タグ + KEV CVE + actor) — 無印 medium を押し退けて残るべき
        _seed_article(repo, run_id, article_id="sig1", importance="medium")
        repo.add_article_entities(
            "sig1", [("pir", "pir_x"), ("actor", "apt_x"), ("cve", "CVE-2026-0001")]
        )
        # high は無条件で上位
        _seed_article(repo, run_id, article_id="hi1", importance="high")
        monkeypatch.setattr(mod, "_kev_set_failsafe", lambda: frozenset({"CVE-2026-0001"}))

        result = fetch_for_deep_dive_candidates(db_path=db_path)
        ids = [c.article_id for c in result.candidates]
        assert len(ids) == RUBRIC_POOL_MAX
        assert "hi1" in ids
        assert "sig1" in ids

    def test_kev_failure_degrades_gracefully(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.digest import db_filter as mod

        def _boom() -> frozenset[str]:
            raise RuntimeError("kev down")

        # _kev_set_failsafe 自体の fail-safe を直接検証
        monkeypatch.setattr("src.tools.kev_client.get_kev_cve_set", _boom, raising=False)
        assert mod._kev_set_failsafe() == frozenset()
