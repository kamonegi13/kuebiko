"""出典基盤エンジン (S1) のテスト — 信頼度は source メタから決定的に算出。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.cti.source_basis import classify_source_tier, compute_source_basis
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "sb.db")


def _seed(
    repo: RunHistoryRepository,
    run_id: int,
    aid: str,
    *,
    feed_title: str,
    feed_url: str,
    cves: list[str] | None = None,
) -> None:
    repo.add_article(
        ArticleRecord(
            run_id=run_id,
            article_id=aid,
            title="t",
            url=f"https://e/{aid}",
            feed_title=feed_title,
            feed_url=feed_url,
            status="posted",
        )
    )
    if cves:
        repo.add_article_entities(aid, [("cve", c) for c in cves], when=datetime.now(UTC))


class TestClassifyTier:
    def test_social(self) -> None:
        assert classify_source_tier("Grok", "https://grok.com/") == "social"
        assert classify_source_tier("@someone", "https://x.com/foo") == "social"

    def test_official(self) -> None:
        assert classify_source_tier("CISA", "https://www.cisa.gov/news/") == "official"
        assert classify_source_tier("NCSC NZ", "https://www.ncsc.govt.nz/news/") == "official"

    def test_research(self) -> None:
        assert classify_source_tier("Mandiant", "https://www.mandiant.com/x") == "research"
        assert classify_source_tier("dragos", "https://www.dragos.com/blog") == "research"

    def test_news_default(self) -> None:
        assert classify_source_tier("BleepingComputer", "https://bleepingcomputer.com") == "news"

    def test_state_media_flagged(self) -> None:
        """国営/影響工作は state_media に分類 (framing 割引対象)。"""
        sm = "state_media"
        assert classify_source_tier("RT (Russia Today)", "https://www.rt.com/news/x") == sm
        assert classify_source_tier("Sputnik Globe (Russia)", "https://sputnikglobe.com/x") == sm
        assert classify_source_tier("Pravda Netherlands", "https://news-pravda.com/x") == sm

    def test_independent_adversary_sources_not_flagged(self) -> None:
        """踏んではいけない罠: 国籍でなく国家統制で分類。独立系は state_media にしない。"""
        # The Insider = 反クレムリン独立調査報道 / NK News = 独立 NK ウォッチャー
        assert classify_source_tier("The Insider (Russia, 英)", "https://theins.ru/x") == "news"
        assert classify_source_tier("NK News", "https://www.nknews.org/x") == "news"
        # SCMP = 北京寄りだが純粋な国営でない (lean ≠ state) → 据え置き
        assert classify_source_tier("South China Morning Post", "https://www.scmp.com/x") == "news"

    def test_unknown_when_empty(self) -> None:
        assert classify_source_tier("", "") == "unknown"

    def test_config_promotes_research_sources(self) -> None:
        """S3: managed config が実 feed の脅威リサーチを news catch-all から research に是正。"""
        # host 経由 (AhnLab ASEC / SOCRadar 系)
        assert classify_source_tier("AhnLab ASEC", "https://asec.ahnlab.com/x") == "research"
        assert classify_source_tier("FortiGuard Labs", "https://feeds.fortinet.com/x") == "research"
        # title 経由 (feed_url 無し source)
        assert classify_source_tier("Threat Intelligence Blog | Flashpoint", "") == "research"
        # 地政学シンクタンク
        assert classify_source_tier("ISW", "https://understandingwar.org/x") == "research"

    def test_config_official_sources(self) -> None:
        """S3: CERT/規制当局を official に。"""
        assert classify_source_tier("ANSSI France", "https://www.cert.ssi.gouv.fr/x") == "official"
        assert classify_source_tier("CERT Polska", "https://cert.pl/x") == "official"


class TestComputeSourceBasis:
    def test_social_single_is_low(self, repo: RunHistoryRepository) -> None:
        run_id = repo.start_run(
            RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False)
        )
        _seed(repo, run_id, "g1", feed_title="Grok", feed_url="https://grok.com/")
        sb = compute_source_basis(repo, ["g1"], kev_set=frozenset())
        assert sb.confidence == "low"
        assert sb.social_only is True
        assert sb.source_count == 1
        assert "SNS" in sb.reason

    def test_official_is_high(self, repo: RunHistoryRepository) -> None:
        run_id = repo.start_run(
            RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False)
        )
        _seed(repo, run_id, "c1", feed_title="CISA", feed_url="https://www.cisa.gov/a")
        sb = compute_source_basis(repo, ["c1"], kev_set=frozenset())
        assert sb.confidence == "high"
        assert sb.best_tier == "official"
        assert sb.has_official_authority is True

    def test_state_media_source_does_not_crash(self, repo: RunHistoryRepository) -> None:
        """回帰防止: state_media tier 追加で compute_source_basis (read 経路) が
        _TIER_RANK/_TIER_LABEL の KeyError で 500 にならないこと (synthesis 表示が落ちた)。"""
        run_id = repo.start_run(
            RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False)
        )
        _seed(repo, run_id, "sm1", feed_title="RT (Russia Today)", feed_url="https://rt.com/a")
        sb = compute_source_basis(repo, ["sm1"], kev_set=frozenset())
        assert sb.best_tier == "state_media"
        assert sb.confidence in ("low", "medium")  # 国営単独は権威に昇格しない
        assert isinstance(sb.reason, str)

    def test_two_research_sources_is_high(self, repo: RunHistoryRepository) -> None:
        run_id = repo.start_run(
            RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False)
        )
        _seed(repo, run_id, "m1", feed_title="Mandiant", feed_url="https://mandiant.com/1")
        _seed(repo, run_id, "d1", feed_title="Dragos", feed_url="https://dragos.com/1")
        sb = compute_source_basis(repo, ["m1", "d1"], kev_set=frozenset())
        assert sb.confidence == "high"
        assert sb.source_count == 2

    def test_kev_registered_cve_lifts_confidence(self, repo: RunHistoryRepository) -> None:
        """SNS 単一でも CVE が CISA KEV 登録なら公式権威で確度が上がる。"""
        run_id = repo.start_run(
            RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False)
        )
        _seed(
            repo,
            run_id,
            "g2",
            feed_title="Grok",
            feed_url="https://grok.com/",
            cves=["CVE-2026-9999"],
        )
        sb = compute_source_basis(repo, ["g2"], kev_set=frozenset({"CVE-2026-9999"}))
        assert sb.has_official_authority is True
        assert sb.confidence == "high"
        assert "CISA KEV" in sb.reason
        assert "CVE-2026-9999" in sb.kev_cves

    def test_news_single_is_medium(self, repo: RunHistoryRepository) -> None:
        run_id = repo.start_run(
            RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False)
        )
        _seed(
            repo,
            run_id,
            "n1",
            feed_title="BleepingComputer",
            feed_url="https://bleepingcomputer.com/x",
        )
        sb = compute_source_basis(repo, ["n1"], kev_set=frozenset())
        assert sb.confidence == "medium"

    def test_empty_ids(self, repo: RunHistoryRepository) -> None:
        sb = compute_source_basis(repo, [], kev_set=frozenset())
        assert sb.confidence == "low"
        assert sb.source_count == 0


class TestCrossSourceCorroboration:
    """S4: 共有 CVE を持つ一次/研究 source を逆引きして裏取りを機械的に検証。

    誤帰属を防ぐ 2 条件: primary が social-only (多トピック束ね) は不可 / 単一 CVE のみ。
    """

    def test_news_single_lifted_by_official_corroboration(self, repo: RunHistoryRepository) -> None:
        """単一 CVE の news 主張を CISA+Mandiant が裏取り → high。"""
        run_id = repo.start_run(
            RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False)
        )
        _seed(
            repo,
            run_id,
            "n",
            feed_title="BleepingComputer",
            feed_url="https://bleepingcomputer.com/x",
            cves=["CVE-2026-7"],
        )
        _seed(
            repo,
            run_id,
            "c",
            feed_title="CISA",
            feed_url="https://www.cisa.gov/a",
            cves=["CVE-2026-7"],
        )
        _seed(
            repo,
            run_id,
            "m",
            feed_title="Mandiant",
            feed_url="https://mandiant.com/x",
            cves=["CVE-2026-7"],
        )

        sb0 = compute_source_basis(repo, ["n"], kev_set=frozenset())
        assert sb0.confidence == "medium"  # news 単独
        assert sb0.source_count == 1

        sb = compute_source_basis(repo, ["n"], kev_set=frozenset(), corroborate_window_days=14)
        assert sb.confidence == "high"  # CISA/Mandiant が裏取り
        assert sb.source_count == 3
        assert sb.best_tier == "official"
        assert "裏取り" in sb.reason

    def test_social_primary_not_corroborated(self, repo: RunHistoryRepository) -> None:
        """Grok (多トピック束ね) は CVE 併記の裏取りで上昇させない (entity を主張に帰属不可)。"""
        run_id = repo.start_run(
            RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False)
        )
        _seed(
            repo, run_id, "g", feed_title="Grok", feed_url="https://grok.com/", cves=["CVE-2026-7"]
        )
        _seed(
            repo,
            run_id,
            "c",
            feed_title="CISA",
            feed_url="https://www.cisa.gov/a",
            cves=["CVE-2026-7"],
        )
        sb = compute_source_basis(repo, ["g"], kev_set=frozenset(), corroborate_window_days=14)
        assert sb.confidence == "low"  # social skip
        assert sb.source_count == 1

    def test_multi_cve_not_corroborated(self, repo: RunHistoryRepository) -> None:
        """複数 CVE 併記 = roundup/bundle でどの CVE が主張か曖昧 → corroboration しない。"""
        run_id = repo.start_run(
            RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False)
        )
        _seed(
            repo,
            run_id,
            "n",
            feed_title="BleepingComputer",
            feed_url="https://bleepingcomputer.com/x",
            cves=["CVE-2026-7", "CVE-2026-8"],
        )
        _seed(
            repo,
            run_id,
            "c",
            feed_title="CISA",
            feed_url="https://www.cisa.gov/a",
            cves=["CVE-2026-7"],
        )
        sb = compute_source_basis(repo, ["n"], kev_set=frozenset(), corroborate_window_days=14)
        assert sb.source_count == 1  # multi-CVE → 裏取りしない
        assert sb.confidence == "medium"

    def test_news_corroborators_not_counted(self, repo: RunHistoryRepository) -> None:
        """裏取りは一次/研究層のみ。ニュース層の CVE 併記は wire 再掲が多く数えない。"""
        run_id = repo.start_run(
            RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False)
        )
        _seed(
            repo,
            run_id,
            "n",
            feed_title="BleepingComputer",
            feed_url="https://bleepingcomputer.com/x",
            cves=["CVE-2026-5"],
        )
        _seed(
            repo,
            run_id,
            "n2",
            feed_title="Defense News",
            feed_url="https://www.defensenews.com/1",
            cves=["CVE-2026-5"],
        )
        sb = compute_source_basis(repo, ["n"], kev_set=frozenset(), corroborate_window_days=14)
        assert sb.source_count == 1  # ニュース併記は裏取りに数えない
        assert sb.confidence == "medium"

    def test_no_corroboration_when_no_shared_entity(self, repo: RunHistoryRepository) -> None:
        run_id = repo.start_run(
            RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False)
        )
        _seed(
            repo,
            run_id,
            "n",
            feed_title="BleepingComputer",
            feed_url="https://bleepingcomputer.com/x",
            cves=["CVE-2026-8"],
        )
        _seed(
            repo,
            run_id,
            "c",
            feed_title="CISA",
            feed_url="https://www.cisa.gov/a",
            cves=["CVE-2026-99"],
        )
        sb = compute_source_basis(repo, ["n"], kev_set=frozenset(), corroborate_window_days=14)
        assert sb.confidence == "medium"  # 別 CVE のため裏取りなし、news 単独
        assert sb.source_count == 1


class TestReliabilityOverride:
    """S3: per-source 信頼度ティアの UI 上書き (raw YAML 不要・override→pattern)。"""

    @pytest.fixture(autouse=True)
    def _clean_cache(self) -> object:
        from src.cti import source_basis as sb

        sb.invalidate_reliability_cache()
        yield
        sb.invalidate_reliability_cache()

    def test_override_set_overrides_pattern(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.cti import source_basis as sb

        monkeypatch.setattr(sb, "_OVERRIDES_PATH", tmp_path / "ov.yaml")
        sb.invalidate_reliability_cache()
        url = "https://bleepingcomputer.com/x"
        assert sb.classify_source_tier("BleepingComputer", url) == "news"  # pattern default

        assert sb.set_source_reliability_override(url, "research") == "research"
        assert sb.classify_source_tier("BleepingComputer", url) == "research"  # 上書き

        assert sb.set_source_reliability_override(url, "auto") == "auto"  # 解除
        assert sb.classify_source_tier("BleepingComputer", url) == "news"  # pattern に復帰

    def test_invalid_tier_is_treated_as_clear(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.cti import source_basis as sb

        monkeypatch.setattr(sb, "_OVERRIDES_PATH", tmp_path / "ov.yaml")
        sb.invalidate_reliability_cache()
        url = "https://bleepingcomputer.com/x"
        sb.set_source_reliability_override(url, "research")
        assert sb.set_source_reliability_override(url, "bogus") == "auto"  # 不正値は解除
        assert sb.classify_source_tier("BleepingComputer", url) == "news"

    def test_empty_key_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.cti import source_basis as sb

        monkeypatch.setattr(sb, "_OVERRIDES_PATH", tmp_path / "ov.yaml")
        with pytest.raises(ValueError):
            sb.set_source_reliability_override("  ", "research")
