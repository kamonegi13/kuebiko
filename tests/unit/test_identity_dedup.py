"""src.cti.identity_dedup のテスト (2026-08-19 投稿直前 dedup ゲート統合)。

4 層 (dedup_key 完全一致 / CVE 正規化 / content 署名 / victim_org) を個別に、および
``check_pre_post_dedup`` の優先順位 (short-circuit) を検証する。dedup_key/CVE/
victim_org は Step 1-3 の機械的抽出 + 新規追加であり、挙動を実 DB (tmp SQLite) で
固定する。content 層はアルゴリズム自体を再検証せず (test_content_dedup.py の責務)、
呼出制御 (skip 判定 / 例外握り潰し) のみを monkeypatch で検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from src.cti.identity_dedup import (
    DedupGateResult,
    check_content_duplicate,
    check_cve_duplicate,
    check_dedup_key_duplicate,
    check_pre_post_dedup,
    check_victim_org_duplicate,
)
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord
from src.tools.article_model import Article
from src.tools.discord_publisher import BriefingMessage


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "identity_dedup.db")


def _now() -> datetime:
    return datetime.now(UTC)


def _msg(
    *,
    title: str = "Sample advisory",
    category: str = "advisory",
    metadata: dict[str, Any] | None = None,
) -> BriefingMessage:
    return BriefingMessage(
        title=title,
        importance="medium",
        category=category,
        summary="summary text",
        metadata=metadata or {},
    )


def _article(*, article_id: str = "art-1", url: str = "https://example.com/a") -> Article:
    return Article(
        id=article_id,
        title="Sample",
        url=url,
        summary_html="<p>body</p>",
        published=datetime(2026, 1, 1, tzinfo=UTC),
        feed_title="Feed",
        feed_url="https://example.com/feed",
    )


def _seed_article(
    repo: RunHistoryRepository,
    *,
    article_id: str,
    dedup_key: str | None = None,
    victim_org: str | None = None,
    category: str = "breach",
    hours_ago: float = 1.0,
    status: str = "posted",
    posted_channel: str = "brief",
) -> None:
    run_id = repo.start_run(RunRecord(started_at=_now(), pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=run_id,
            article_id=article_id,
            title="prior article",
            url=f"https://example.com/{article_id}",
            status=status,  # type: ignore[arg-type]
            category=category,
            posted_channel=posted_channel,
            dedup_key=dedup_key,
            created_at=_now() - timedelta(hours=hours_ago),
        ),
    )
    if victim_org:
        repo.add_article_entities(article_id, [("victim_org", victim_org)])


class TestCheckDedupKeyDuplicate:
    def test_no_dedup_key_in_metadata_passes_through(self, repo: RunHistoryRepository) -> None:
        # Arrange
        msg = _msg(metadata={})
        seen: set[str] = set()

        # Act
        result = check_dedup_key_duplicate(
            msg=msg, art_id="new", channel="brief", dedup_repo=repo, cross_channel_seen_keys=seen
        )

        # Assert
        assert result is None
        assert seen == set()

    def test_same_key_seen_earlier_in_run_is_skipped(self, repo: RunHistoryRepository) -> None:
        # Arrange
        msg = _msg(metadata={"dedup_key": "k1"})
        seen = {"k1"}

        # Act
        result = check_dedup_key_duplicate(
            msg=msg, art_id="new", channel="brief", dedup_repo=repo, cross_channel_seen_keys=seen
        )

        # Assert
        assert isinstance(result, DedupGateResult)
        assert "same key in this run" in result.failure_reason

    def test_prior_post_within_48h_is_skipped(self, repo: RunHistoryRepository) -> None:
        # Arrange
        _seed_article(repo, article_id="prev", dedup_key="k2", hours_ago=2)
        msg = _msg(metadata={"dedup_key": "k2"})
        seen: set[str] = set()

        # Act
        result = check_dedup_key_duplicate(
            msg=msg, art_id="new", channel="brief", dedup_repo=repo, cross_channel_seen_keys=seen
        )

        # Assert
        assert isinstance(result, DedupGateResult)
        assert "prior post 48h" in result.failure_reason

    def test_prior_post_outside_48h_is_not_skipped_and_key_is_recorded(
        self, repo: RunHistoryRepository
    ) -> None:
        # Arrange: 10 日前 (48h 窓の外、かつ calendar day 境界の曖昧さを避けるため
        # 十分離す — created_at の ISO 'T' 区切りと SQLite datetime() の空白区切りの
        # 表記差で、同一暦日内だと文字列比較が誤って揃うケースがあるため)
        _seed_article(repo, article_id="prev", dedup_key="k3", hours_ago=24 * 10)
        msg = _msg(metadata={"dedup_key": "k3"})
        seen: set[str] = set()

        # Act
        result = check_dedup_key_duplicate(
            msg=msg, art_id="new", channel="brief", dedup_repo=repo, cross_channel_seen_keys=seen
        )

        # Assert
        assert result is None
        assert "k3" in seen


class TestCheckCveDuplicate:
    def test_no_dedup_repo_passes_through(self) -> None:
        msg = _msg(title="Apache CVE-2026-9999 RCE")
        result = check_cve_duplicate(
            msg=msg, art_id="new", channel="brief", dedup_repo=None, cross_channel_seen_cves=set()
        )
        assert result is None

    def test_same_cve_seen_earlier_in_run_is_skipped(self, repo: RunHistoryRepository) -> None:
        # Arrange
        msg = _msg(title="Apache CVE-2026-9999 RCE")
        seen = {"cve-2026-9999"}

        # Act
        result = check_cve_duplicate(
            msg=msg, art_id="new", channel="brief", dedup_repo=repo, cross_channel_seen_cves=seen
        )

        # Assert
        assert isinstance(result, DedupGateResult)
        assert "same CVE in this run" in result.failure_reason

    def test_prior_post_within_48h_is_skipped(self, repo: RunHistoryRepository) -> None:
        # Arrange
        _seed_article(repo, article_id="prev", dedup_key="cve-2026-1234", hours_ago=2)
        msg = _msg(title="Vendor patches CVE-2026-1234")

        # Act
        result = check_cve_duplicate(
            msg=msg,
            art_id="new",
            channel="brief",
            dedup_repo=repo,
            cross_channel_seen_cves=set(),
        )

        # Assert
        assert isinstance(result, DedupGateResult)
        assert "prior post 48h" in result.failure_reason

    def test_no_cve_in_title_or_key_passes_through(self, repo: RunHistoryRepository) -> None:
        msg = _msg(title="Generic threat intel report")
        seen: set[str] = set()

        result = check_cve_duplicate(
            msg=msg, art_id="new", channel="brief", dedup_repo=repo, cross_channel_seen_cves=seen
        )

        assert result is None
        assert seen == set()


class TestCheckContentDuplicate:
    """アルゴリズム (Jaccard) は test_content_dedup.py の責務。ここは呼出制御のみ。"""

    def test_no_dedup_repo_passes_through(self) -> None:
        msg = _msg()
        result = check_content_duplicate(
            msg=msg, art_id="new", channel="brief", dedup_repo=None, article=None
        )
        assert result is None

    def test_match_found_is_skipped_with_prior_info(
        self, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        prior = ArticleRecord(
            run_id=1,
            article_id="prior-art",
            title="prior",
            url="https://example.com/prior",
            feed_title="Other Feed",
            status="posted",
            dedup_key="some-key",
        )
        monkeypatch.setattr(
            "src.cti.identity_dedup.find_recent_content_duplicate",
            lambda **kwargs: prior,
        )
        msg = _msg()

        # Act
        result = check_content_duplicate(
            msg=msg,
            art_id="new",
            channel="brief",
            dedup_repo=repo,
            article=_article(),
        )

        # Assert
        assert isinstance(result, DedupGateResult)
        assert "Other Feed" in result.failure_reason
        assert "some-key" in result.failure_reason

    def test_no_match_passes_through(
        self, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "src.cti.identity_dedup.find_recent_content_duplicate",
            lambda **kwargs: None,
        )
        result = check_content_duplicate(
            msg=_msg(), art_id="new", channel="brief", dedup_repo=repo, article=_article()
        )
        assert result is None

    def test_lookup_exception_is_swallowed(
        self, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(**kwargs: object) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr("src.cti.identity_dedup.find_recent_content_duplicate", _raise)

        result = check_content_duplicate(
            msg=_msg(), art_id="new", channel="brief", dedup_repo=repo, article=_article()
        )

        assert result is None


class TestCheckVictimOrgDuplicate:
    def test_non_breach_incident_category_is_not_checked(self, repo: RunHistoryRepository) -> None:
        # Arrange: 直近 posted で同一 org があっても category が対象外なら skip しない
        _seed_article(repo, article_id="prev", victim_org="Acme Corp", category="apt", hours_ago=1)
        msg = _msg(category="apt", metadata={"victim_orgs": ["Acme Corp"]})

        # Act
        result = check_victim_org_duplicate(msg=msg, art_id="new", channel="brief", dedup_repo=repo)

        # Assert
        assert result is None

    @pytest.mark.parametrize("category", ["breach", "incident"])
    def test_match_within_24h_is_skipped(self, repo: RunHistoryRepository, category: str) -> None:
        # Arrange
        _seed_article(
            repo, article_id="prev", victim_org="Acme Corp", category=category, hours_ago=2
        )
        msg = _msg(category=category, metadata={"victim_orgs": ["Acme Corp"]})

        # Act
        result = check_victim_org_duplicate(msg=msg, art_id="new", channel="brief", dedup_repo=repo)

        # Assert
        assert isinstance(result, DedupGateResult)
        assert "Acme Corp" in result.failure_reason
        assert "prev" in result.failure_reason

    def test_match_outside_24h_is_not_skipped(self, repo: RunHistoryRepository) -> None:
        """24h 超の続報は正当な情報として通す。"""
        # Arrange
        _seed_article(
            repo, article_id="prev", victim_org="Acme Corp", category="breach", hours_ago=25
        )
        msg = _msg(category="breach", metadata={"victim_orgs": ["Acme Corp"]})

        # Act
        result = check_victim_org_duplicate(msg=msg, art_id="new", channel="brief", dedup_repo=repo)

        # Assert
        assert result is None

    def test_different_org_is_not_skipped(self, repo: RunHistoryRepository) -> None:
        """表記ゆれは lower/trim のみ吸収、部分一致・fuzzy はしない。"""
        # Arrange
        _seed_article(
            repo, article_id="prev", victim_org="Acme Corp", category="breach", hours_ago=1
        )
        msg = _msg(category="breach", metadata={"victim_orgs": ["Acme Corporation"]})

        # Act
        result = check_victim_org_duplicate(msg=msg, art_id="new", channel="brief", dedup_repo=repo)

        # Assert
        assert result is None

    def test_case_whitespace_variation_is_skipped(self, repo: RunHistoryRepository) -> None:
        # Arrange
        _seed_article(
            repo, article_id="prev", victim_org=" Acme Corp ", category="breach", hours_ago=1
        )
        msg = _msg(category="breach", metadata={"victim_orgs": ["acme corp"]})

        # Act
        result = check_victim_org_duplicate(msg=msg, art_id="new", channel="brief", dedup_repo=repo)

        # Assert
        assert isinstance(result, DedupGateResult)

    def test_flag_off_disables_check(
        self, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        monkeypatch.setenv("VICTIM_ORG_DEDUP", "0")
        _seed_article(
            repo, article_id="prev", victim_org="Acme Corp", category="breach", hours_ago=1
        )
        msg = _msg(category="breach", metadata={"victim_orgs": ["Acme Corp"]})

        # Act
        result = check_victim_org_duplicate(msg=msg, art_id="new", channel="brief", dedup_repo=repo)

        # Assert
        assert result is None

    def test_no_dedup_repo_passes_through(self) -> None:
        msg = _msg(category="breach", metadata={"victim_orgs": ["Acme Corp"]})
        result = check_victim_org_duplicate(msg=msg, art_id="new", channel="brief", dedup_repo=None)
        assert result is None

    def test_no_victim_orgs_metadata_passes_through(self, repo: RunHistoryRepository) -> None:
        msg = _msg(category="breach", metadata={})
        result = check_victim_org_duplicate(msg=msg, art_id="new", channel="brief", dedup_repo=repo)
        assert result is None

    def test_repo_lookup_exception_is_swallowed(self, repo: RunHistoryRepository) -> None:
        class _RaisingRepo:
            def find_recent_posted_article_by_victim_org(
                self, *args: object, **kwargs: object
            ) -> None:
                raise RuntimeError("db down")

        msg = _msg(category="breach", metadata={"victim_orgs": ["Acme Corp"]})

        result = check_victim_org_duplicate(
            msg=msg,
            art_id="new",
            channel="brief",
            dedup_repo=cast(RunHistoryRepository, _RaisingRepo()),
        )

        assert result is None


class TestCheckPrePostDedupOrdering:
    """短絡評価の優先順位 (dedup_key → CVE → content → victim_org) を固定する。"""

    def test_dedup_key_match_short_circuits_before_victim_org(
        self, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: dedup_key と victim_org の両方が一致する状況を作る
        monkeypatch.setattr(
            "src.cti.identity_dedup.find_recent_content_duplicate", lambda **kwargs: None
        )
        _seed_article(repo, article_id="prev-key", dedup_key="shared-key", hours_ago=1)
        _seed_article(
            repo, article_id="prev-org", victim_org="Acme Corp", category="breach", hours_ago=1
        )
        msg = _msg(
            category="breach",
            metadata={"dedup_key": "shared-key", "victim_orgs": ["Acme Corp"]},
        )

        # Act
        result = check_pre_post_dedup(
            msg=msg,
            art_id="new",
            channel="brief",
            dedup_repo=repo,
            article=None,
            cross_channel_seen_keys=set(),
            cross_channel_seen_cves=set(),
        )

        # Assert: dedup_key 層 (最優先) の failure_reason が返る
        assert result is not None
        assert result.failure_reason.startswith("cross-ch dedup")

    def test_no_layer_matches_returns_none(
        self, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "src.cti.identity_dedup.find_recent_content_duplicate", lambda **kwargs: None
        )
        msg = _msg(category="advisory", metadata={})

        result = check_pre_post_dedup(
            msg=msg,
            art_id="new",
            channel="brief",
            dedup_repo=repo,
            article=None,
            cross_channel_seen_keys=set(),
            cross_channel_seen_cves=set(),
        )

        assert result is None

    def test_victim_org_layer_reached_when_earlier_layers_pass(
        self, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "src.cti.identity_dedup.find_recent_content_duplicate", lambda **kwargs: None
        )
        _seed_article(
            repo, article_id="prev-org", victim_org="Acme Corp", category="breach", hours_ago=1
        )
        msg = _msg(category="breach", metadata={"victim_orgs": ["Acme Corp"]})

        result = check_pre_post_dedup(
            msg=msg,
            art_id="new",
            channel="brief",
            dedup_repo=repo,
            article=None,
            cross_channel_seen_keys=set(),
            cross_channel_seen_cves=set(),
        )

        assert result is not None
        assert result.failure_reason.startswith("victim_org dedup")
