"""Phase H: taxonomy review pipeline のテスト。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.storage.run_history import (
    ArticleRecord,
    RunHistoryRepository,
    RunRecord,
    TaxonomyProposalRecord,
)
from src.taxonomy.proposal_generator import (
    _levenshtein,
    _looks_like_typo,
    detect_typo_candidates,
    detect_uncategorized_clusters,
)
from src.taxonomy.runner import run_taxonomy_review


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RunHistoryRepository:
    # PG 接続を抑止して test 中は SQLite を使わせる (db_backend は DATABASE_URL 優先のため)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return RunHistoryRepository(db_path=tmp_path / "tax.db")


def _now() -> datetime:
    return datetime.now(UTC)


class TestLevenshtein:
    def test_identical(self) -> None:
        assert _levenshtein("financial", "financial") == 0

    def test_one_typo(self) -> None:
        assert _levenshtein("militry", "military") == 1

    def test_one_typo_substitution(self) -> None:
        assert _levenshtein("financla", "financial") == 2

    def test_unrelated(self) -> None:
        d = _levenshtein("financial", "aerospace")
        assert d > 3

    def test_case_insensitive(self) -> None:
        # _levenshtein は内部で .lower() しているので大文字でも 0
        assert _levenshtein("FINANCE", "finance") == 0


class TestLooksLikeTypo:
    def test_long_ascii_typo_accepted(self) -> None:
        # 長い ASCII の 1 編集 typo は採用 (距離 1 / 長さ 10 = 0.1)
        assert _looks_like_typo("govenment", "government")[0] is True
        assert _looks_like_typo("militry", "military")[0] is True

    def test_short_cjk_false_pair_rejected(self) -> None:
        # 2 文字 CJK の無関係ペアは長さ比 (2/2=1.0) で却下 (誤爆の根治)
        assert _looks_like_typo("航空", "金融")[0] is False
        assert _looks_like_typo("化学", "教育")[0] is False

    def test_identical_not_typo(self) -> None:
        assert _looks_like_typo("financial", "financial")[0] is False

    def test_beyond_max_distance_rejected(self) -> None:
        assert _looks_like_typo("financial", "aerospace")[0] is False


class TestTaxonomyProposalRecordPersistence:
    def test_insert_and_list(self, repo: RunHistoryRepository) -> None:
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="weekly-taxonomy-review", dry_run=False),
        )
        record = TaxonomyProposalRecord(
            run_id=run_id,
            proposal_type="pattern_4",
            tier="tier_1_auto",
            target_yaml="victim_sectors",
            target_canonical="military",
            proposed_change='{"kind":"add_alias","alias":"militry","canonical":"military"}',
            rationale="typo 距離 1",
            confidence="high",
            evidence_count=3,
            evidence_ids='["a1","a2","a3"]',
        )
        pid = repo.insert_taxonomy_proposal(record)
        assert pid > 0
        listed = repo.list_taxonomy_proposals()
        assert len(listed) == 1
        assert listed[0].proposal_type == "pattern_4"
        assert listed[0].tier == "tier_1_auto"

    def test_find_pending_proposal_null_target_canonical(self, repo: RunHistoryRepository) -> None:
        # 2026-07-06 PG dialect fix の回帰: target_canonical=None での merge lookup。
        # 旧実装は `? IS NULL` で PG が param 型を推論できず weekly-taxonomy-review が
        # 毎週失敗していた (SQLite では動くため tests をすり抜けた)。ここでは NULL 一致
        # ロジックの意味的正しさを固定する (PG 型修正は本番 PG で別途実証済)。
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="weekly-taxonomy-review", dry_run=False),
        )
        repo.insert_taxonomy_proposal(
            TaxonomyProposalRecord(
                run_id=run_id,
                proposal_type="pattern_1",
                tier="tier_3_strategic",
                target_yaml="config/cti/victim_sectors.yaml",
                target_canonical=None,  # 新規 canonical 提案は既存を指さない
                proposed_change='{"kind":"new_canonical","name":"space_defense"}',
                rationale="新規セクター候補",
                confidence="low",
                evidence_count=2,
                evidence_ids='["a1","a2"]',
            ),
        )
        # None で検索 → NULL 同士が一致する
        found = repo.find_pending_proposal(
            proposal_type="pattern_1",
            target_yaml="config/cti/victim_sectors.yaml",
            target_canonical=None,
            proposed_change='{"kind":"new_canonical","name":"space_defense"}',
        )
        assert found is not None
        assert found.target_canonical is None
        # 非 None で検索 → NULL 行には一致しない (意味的に別物)
        other = repo.find_pending_proposal(
            proposal_type="pattern_1",
            target_yaml="config/cti/victim_sectors.yaml",
            target_canonical="space_defense",
            proposed_change='{"kind":"new_canonical","name":"space_defense"}',
        )
        assert other is None

    def test_filter_by_status(self, repo: RunHistoryRepository) -> None:
        for status in ("pending", "accepted", "rejected"):
            repo.insert_taxonomy_proposal(
                TaxonomyProposalRecord(
                    proposal_type="pattern_1",
                    tier="tier_2_review",
                    target_yaml="victim_sectors",
                    proposed_change="{}",
                    rationale="x",
                    confidence="medium",
                    status=status,
                ),
            )
        assert len(repo.list_taxonomy_proposals(status="pending")) == 1
        assert len(repo.list_taxonomy_proposals(status="accepted")) == 1
        assert len(repo.list_taxonomy_proposals(status="rejected")) == 1

    def test_update_status_marks_reviewed(self, repo: RunHistoryRepository) -> None:
        pid = repo.insert_taxonomy_proposal(
            TaxonomyProposalRecord(
                proposal_type="pattern_4",
                tier="tier_1_auto",
                target_yaml="victim_sectors",
                proposed_change="{}",
                rationale="x",
                confidence="high",
            ),
        )
        assert repo.update_taxonomy_proposal_status(pid, status="accepted") is True
        loaded = repo.get_taxonomy_proposal(pid)
        assert loaded is not None
        assert loaded.status == "accepted"
        assert loaded.reviewed_at is not None

    def test_find_pending_for_merge(self, repo: RunHistoryRepository) -> None:
        repo.insert_taxonomy_proposal(
            TaxonomyProposalRecord(
                proposal_type="pattern_4",
                tier="tier_1_auto",
                target_yaml="victim_sectors",
                target_canonical="military",
                proposed_change='{"alias":"militry"}',
                rationale="x",
                confidence="high",
            ),
        )
        existing = repo.find_pending_proposal(
            proposal_type="pattern_4",
            target_yaml="victim_sectors",
            target_canonical="military",
            proposed_change='{"alias":"militry"}',
        )
        assert existing is not None
        # 違う proposed_change ならヒットしない
        miss = repo.find_pending_proposal(
            proposal_type="pattern_4",
            target_yaml="victim_sectors",
            target_canonical="military",
            proposed_change='{"alias":"different"}',
        )
        assert miss is None


class TestTypoDetection:
    def test_detects_typo_with_canonical_alias(
        self,
        repo: RunHistoryRepository,
        tmp_path: Path,
    ) -> None:
        from src.cti.taxonomy_normalizer import load_normalizer

        normalizer = load_normalizer()  # 本番 yaml を読む (financial/military 等が含まれる)
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="x", dry_run=False),
        )
        # "militry" を 3 件、uncategorized で投入 → military typo として検出されるはず
        for i in range(3):
            repo.add_article(
                ArticleRecord(
                    run_id=run_id,
                    article_id=f"art-{i}",
                    title="t",
                    url=f"https://example.com/{i}",
                    status="posted",
                    victim_sector_canonical="uncategorized",
                    victim_sector_raw="militry",
                ),
            )
        proposals = detect_typo_candidates(
            normalizer=normalizer,
            db_path=repo._db_path,  # noqa: SLF001
            lookback_days=30,
        )
        # "militry" は "military" alias 経由で "defense" canonical にマップされる
        types = [(p.proposal_type, p.target_canonical) for p in proposals]
        assert ("pattern_4", "defense") in types

    def test_no_typo_for_short_cjk_false_positive(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        # 回帰: 2 文字 CJK 語 (航空/化学) は任意の別 2 文字 alias と距離 2 以内になり
        # 無関係 canonical (financial/education) へ typo 誤判定されていた (長さ比ゲートで根治)。
        from src.cti.taxonomy_normalizer import load_normalizer

        normalizer = load_normalizer()
        run_id = repo.start_run(RunRecord(started_at=_now(), pipeline="x", dry_run=False))
        for i in range(3):
            repo.add_article(
                ArticleRecord(
                    run_id=run_id,
                    article_id=f"cjk-{i}",
                    title="t",
                    url=f"https://example.com/cjk{i}",
                    status="posted",
                    victim_sector_canonical="uncategorized",
                    victim_sector_raw="航空",
                ),
            )
        proposals = detect_typo_candidates(
            normalizer=normalizer,
            db_path=repo._db_path,  # noqa: SLF001
            lookback_days=30,
        )
        assert not any(p.proposal_type == "pattern_4" for p in proposals)

    def test_no_typo_for_unrelated_raw(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        from src.cti.taxonomy_normalizer import load_normalizer

        normalizer = load_normalizer()
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="x", dry_run=False),
        )
        # 既存 canonical と無関係な新概念
        for i in range(5):
            repo.add_article(
                ArticleRecord(
                    run_id=run_id,
                    article_id=f"art-{i}",
                    title="t",
                    url=f"https://example.com/{i}",
                    status="posted",
                    victim_sector_canonical="uncategorized",
                    victim_sector_raw="zzz-unknown-aerospace-thing",
                ),
            )
        proposals = detect_typo_candidates(
            normalizer=normalizer,
            db_path=repo._db_path,  # noqa: SLF001
            lookback_days=30,
        )
        # typo として検出されない
        assert not any(p.proposal_type == "pattern_4" for p in proposals)


class TestUncategorizedClusterDetection:
    def test_proposes_new_category_for_high_count_unknown(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        from src.cti.taxonomy_normalizer import load_normalizer

        normalizer = load_normalizer()
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="x", dry_run=False),
        )
        for i in range(5):
            repo.add_article(
                ArticleRecord(
                    run_id=run_id,
                    article_id=f"art-{i}",
                    title="t",
                    url=f"https://example.com/{i}",
                    status="posted",
                    victim_sector_canonical="uncategorized",
                    # 2026-07-12: 生成器が「現辞書で解決できる raw」を skip するようになった
                    # (orbital-logistics は fuzzy で critical_infra に解決) ため、真に未解決の値
                    victim_sector_raw="orbital shipyard",
                ),
            )
        proposals = detect_uncategorized_clusters(
            normalizer=normalizer,
            db_path=repo._db_path,  # noqa: SLF001
            lookback_days=30,
            min_count=3,
        )
        names = [p.target_yaml for p in proposals]
        assert "victim_sectors" in names


class TestRunTaxonomyReview:
    def test_dry_run_does_not_persist(self, repo: RunHistoryRepository) -> None:
        result = asyncio.run(run_taxonomy_review(repo=None, lookback_days=30))
        assert result.new_proposals == 0  # dry-run なので 0

    def test_full_run_persists_and_refreshes(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        # typo data を仕込む
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="weekly-taxonomy-review", dry_run=False),
        )
        for i in range(3):
            repo.add_article(
                ArticleRecord(
                    run_id=run_id,
                    article_id=f"art-{i}",
                    title="t",
                    url=f"https://example.com/{i}",
                    status="posted",
                    victim_sector_canonical="uncategorized",
                    victim_sector_raw="militry",
                ),
            )
        # 1 回目: insert
        result_1 = asyncio.run(
            run_taxonomy_review(repo=repo, run_id=run_id, lookback_days=30),
        )
        assert result_1.new_proposals >= 1
        # 2 回目: 同じデータで実行 → refresh
        result_2 = asyncio.run(
            run_taxonomy_review(repo=repo, run_id=run_id, lookback_days=30),
        )
        assert result_2.refreshed_proposals >= 1
        assert result_2.new_proposals == 0  # 新規はなし


class TestNullSentinelGuards:
    """null 番兵の根治 (2026-07-12): 「特定できなかった」の別表記は提案対象にしない。

    'Not Found' 167 件が「新カテゴリ not_found」として毎週 Tier2 に浮上していた回帰を固定。
    上流 (正規化器) の null 化と、lookback 窓内に残る既存行への生成器側 skip の二重防御。
    """

    def _seed(self, repo: RunHistoryRepository, raw: str, n: int) -> None:
        run_id = repo.start_run(RunRecord(started_at=_now(), pipeline="x", dry_run=False))
        for i in range(n):
            repo.add_article(
                ArticleRecord(
                    run_id=run_id,
                    article_id=f"s-{raw}-{i}",
                    title="t",
                    url=f"https://example.com/{raw}/{i}",
                    status="posted",
                    victim_sector_canonical="uncategorized",
                    victim_sector_raw=raw,
                ),
            )

    def test_uncategorized_clusters_skip_null_sentinels(self, repo: RunHistoryRepository) -> None:
        from src.cti.taxonomy_normalizer import load_normalizer

        self._seed(repo, "Not Found", 5)
        self._seed(repo, "不明", 5)
        proposals = detect_uncategorized_clusters(
            normalizer=load_normalizer(),
            db_path=repo._db_path,  # noqa: SLF001
            lookback_days=30,
            min_count=3,
        )
        assert proposals == []

    def test_typo_candidates_skip_null_sentinels(self, repo: RunHistoryRepository) -> None:
        from src.cti.taxonomy_normalizer import load_normalizer

        self._seed(repo, "unknown", 3)
        proposals = detect_typo_candidates(
            normalizer=load_normalizer(),
            db_path=repo._db_path,  # noqa: SLF001
            lookback_days=30,
        )
        assert [p for p in proposals if "unknown" in str(p.proposed_change)] == []

    def test_resolved_raw_no_longer_proposed(self, repo: RunHistoryRepository) -> None:
        # 辞書に学習済み (alias 追加済み) の raw は、窓内に旧 uncategorized 行が
        # 残っていても再提案しない (例: 「交通」= 2026-07-12 に critical_infra alias 化)
        from src.cti.taxonomy_normalizer import load_normalizer

        self._seed(repo, "交通", 5)
        normalizer = load_normalizer()
        clusters = detect_uncategorized_clusters(
            normalizer=normalizer,
            db_path=repo._db_path,  # noqa: SLF001
            lookback_days=30,
            min_count=3,
        )
        typos = detect_typo_candidates(
            normalizer=normalizer,
            db_path=repo._db_path,  # noqa: SLF001
            lookback_days=30,
        )
        assert clusters == []
        assert [p for p in typos if "交通" in str(p.proposed_change)] == []


class TestRejectedNotReproposed:
    """却下は再提案しない (MITRE sync と同原則、2026-07-12)。

    従来は pending のみ照合していたため、却下した同一提案が翌週再 INSERT されていた。
    """

    def test_runner_skips_identical_rejected(self, repo: RunHistoryRepository) -> None:
        run_id = repo.start_run(RunRecord(started_at=_now(), pipeline="x", dry_run=False))
        for i in range(5):
            repo.add_article(
                ArticleRecord(
                    run_id=run_id,
                    article_id=f"orb-{i}",
                    title="t",
                    url=f"https://example.com/orb/{i}",
                    status="posted",
                    victim_sector_canonical="uncategorized",
                    victim_sector_raw="orbital shipyard",
                ),
            )
        # 1 回目: pending 提案が生まれる → 却下する
        first = asyncio.run(run_taxonomy_review(repo=repo, run_id=run_id, lookback_days=30))
        assert first.new_proposals >= 1
        for p in repo.list_taxonomy_proposals(status="pending"):
            assert p.id is not None
            repo.update_taxonomy_proposal_status(p.id, status="rejected")
        # 2 回目: 同一提案は skip され、pending は復活しない
        second = asyncio.run(run_taxonomy_review(repo=repo, run_id=run_id, lookback_days=30))
        assert second.new_proposals == 0
        assert repo.list_taxonomy_proposals(status="pending") == []


class TestRunDuplicateInflation:
    """2026-07-31: 同一 article の run 重複行が観測件数を水増しするバグの regression。"""

    def test_typo_count_is_distinct_articles(self, repo: RunHistoryRepository) -> None:
        from src.cti.taxonomy_normalizer import load_normalizer

        normalizer = load_normalizer()
        # 同一 article_id を 4 run に投入 (再処理相当) — 実記事は 1 件
        for _i in range(4):
            run_id = repo.start_run(
                RunRecord(started_at=_now(), pipeline="x", dry_run=False),
            )
            repo.add_article(
                ArticleRecord(
                    run_id=run_id,
                    article_id="same-article",
                    title="t",
                    url="https://example.com/same",
                    status="posted",
                    victim_sector_canonical="uncategorized",
                    victim_sector_raw="militry",
                ),
            )
        proposals = detect_typo_candidates(
            normalizer=normalizer,
            db_path=repo._db_path,  # noqa: SLF001
            lookback_days=30,
        )
        # 1 記事では TYPO_MIN_OCCURRENCES (2) に満たない → 提案されない
        assert all(p.target_canonical != "defense" for p in proposals)

    def test_evidence_ids_deduped(self, repo: RunHistoryRepository) -> None:
        from src.cti.taxonomy_normalizer import load_normalizer

        normalizer = load_normalizer()
        # 2 記事 (閾値充足) のうち 1 記事は run 重複 3 行
        for _i in range(3):
            run_id = repo.start_run(
                RunRecord(started_at=_now(), pipeline="x", dry_run=False),
            )
            repo.add_article(
                ArticleRecord(
                    run_id=run_id,
                    article_id="dup-article",
                    title="t",
                    url="https://example.com/dup",
                    status="posted",
                    victim_sector_canonical="uncategorized",
                    victim_sector_raw="militry",
                ),
            )
        run_id = repo.start_run(RunRecord(started_at=_now(), pipeline="x", dry_run=False))
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="other-article",
                title="t",
                url="https://example.com/other",
                status="posted",
                victim_sector_canonical="uncategorized",
                victim_sector_raw="militry",
            ),
        )
        proposals = detect_typo_candidates(
            normalizer=normalizer,
            db_path=repo._db_path,  # noqa: SLF001
            lookback_days=30,
        )
        target = next(p for p in proposals if p.target_canonical == "defense")
        assert target.evidence_count == 2
        assert sorted(target.evidence_ids) == ["dup-article", "other-article"]
