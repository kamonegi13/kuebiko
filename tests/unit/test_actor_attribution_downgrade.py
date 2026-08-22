"""帰属の粒度低下検出と別名提案への還流。

信号: タイトルが辞書未収録のアクター名を含むのに、帰属は本文の別の既知アクターへ
LLM 経由で付いている。同一性は判定せず、人の確認材料として起票する。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.cti.actor_attribution_downgrade import (
    detect_attribution_downgrades,
    propose_downgrade_aliases,
)
from src.cti.news_alias_harvest import PROPOSAL_TYPE_NEWS_ALIAS
from src.storage.records import ArticleRecord, RunRecord
from src.storage.run_history import RunHistoryRepository

_NOW = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    r = RunHistoryRepository(db_path=tmp_path / "downgrade.db")
    r.start_run(RunRecord(started_at=_NOW, pipeline="test", dry_run=False))
    return r


def _article(
    repo: RunHistoryRepository,
    article_id: str,
    title: str,
    *,
    provisional: str,
    subject_ids: str | None = None,
    subject_source: str | None = None,
) -> None:
    repo.add_article(
        ArticleRecord(
            run_id=1,
            article_id=article_id,
            title=title,
            url=f"https://kuebiko.example/{article_id}",
            importance="high",
            category="apt",
            status="posted",
            subject_actor_ids=subject_ids,
            subject_actor_source=subject_source,
        )
    )
    repo.add_article_entities(article_id, [("actor_provisional", provisional)])


class TestDetection:
    def test_llm_attribution_to_other_actor_is_flagged(self, repo: RunHistoryRepository) -> None:
        # Arrange
        _article(
            repo,
            "a1",
            "Kuebiko Phantom、中東を標的とした新マルウェアを展開",
            provisional="kuebiko phantom",
            subject_ids="unc1549",
            subject_source="llm",
        )

        # Act
        found = detect_attribution_downgrades(repo, now=_NOW)

        # Assert
        assert [(s.key, s.display, s.attributed_to, s.articles) for s in found] == [
            ("kuebiko phantom", "Kuebiko Phantom", ("unc1549",), 1)
        ]

    def test_title_sourced_attribution_is_normal(self, repo: RunHistoryRepository) -> None:
        # Arrange — タイトル層が既知アクターで発火した分は疑いにしない
        _article(
            repo,
            "a1",
            "Lazarus と Kuebiko Phantom の関係",
            provisional="kuebiko phantom",
            subject_ids="lazarus",
            subject_source="title",
        )

        # Act / Assert
        assert detect_attribution_downgrades(repo, now=_NOW) == []

    def test_unattributed_article_is_not_a_downgrade(self, repo: RunHistoryRepository) -> None:
        # Arrange — 未帰属は「丸め込み」ではない (実測で判別力が無かった母集団)
        _article(repo, "a1", "SilkParasite の解析", provisional="silkparasite",
                 subject_source="none")

        # Act / Assert
        assert detect_attribution_downgrades(repo, now=_NOW) == []

    def test_name_only_in_body_is_not_flagged(self, repo: RunHistoryRepository) -> None:
        # Arrange — タイトルに出ないならタイトル層の取りこぼしではない
        _article(repo, "a1", "無関係な見出し", provisional="kuebiko phantom",
                 subject_ids="unc1549", subject_source="llm")

        # Act / Assert
        assert detect_attribution_downgrades(repo, now=_NOW) == []

    def test_substring_of_longer_word_does_not_match(self, repo: RunHistoryRepository) -> None:
        # Arrange — 語境界を無視すると "pink" が "Pinkerton" を拾う
        _article(repo, "a1", "Pinkerton 社への侵害", provisional="pink",
                 subject_ids="lazarus", subject_source="llm")

        # Act / Assert
        assert detect_attribution_downgrades(repo, now=_NOW) == []

    def test_outside_window_is_excluded(self, repo: RunHistoryRepository) -> None:
        # Arrange
        _article(repo, "a1", "Kuebiko Phantom の活動", provisional="kuebiko phantom",
                 subject_ids="unc1549", subject_source="llm")

        # Act / Assert
        assert detect_attribution_downgrades(
            repo, now=_NOW + timedelta(days=400), window_days=30
        ) == []


class TestProposal:
    def test_proposes_alias_with_identity_warning(self, repo: RunHistoryRepository) -> None:
        # Arrange
        _article(repo, "a1", "Kuebiko Phantom、中東を標的", provisional="kuebiko phantom",
                 subject_ids="unc1549", subject_source="llm")

        # Act
        stats = propose_downgrade_aliases(repo, now=_NOW)

        # Assert
        assert stats["proposed"] == 1
        p = repo.find_actor_update_proposal(
            proposal_type=PROPOSAL_TYPE_NEWS_ALIAS,
            dedup_key="news_alias:unc1549:kuebiko phantom",
        )
        assert p is not None
        assert "同一性は未判定" in p.rationale  # 人が判定する (自動昇格しない)
        assert "粒度低下" in p.rationale
        payload = json.loads(p.payload)
        # 別名はタイトル中の実表記で入る (辞書の既存表記と揃える)
        assert payload["alias"] == "Kuebiko Phantom"
        assert payload["actor_id"] == "unc1549"
        assert payload["_evidence"]["signal"] == "attribution_downgrade"

    def test_split_attribution_is_not_proposed(self, repo: RunHistoryRepository) -> None:
        # Arrange — 帰属先が複数だと「どのアクターの別名か」を問いにできない
        _article(repo, "a1", "Kuebiko Phantom の攻撃", provisional="kuebiko phantom",
                 subject_ids="unc1549,lazarus", subject_source="llm")

        # Act
        stats = propose_downgrade_aliases(repo, now=_NOW)

        # Assert
        assert stats["proposed"] == 0
        assert stats["skipped"] == 1

    def test_known_alias_is_not_proposed(self, repo: RunHistoryRepository) -> None:
        # Arrange — 辞書が既に知っている名前 (暗定側の取りこぼし) は起票しない
        _article(repo, "a1", "Volt Typhoon の活動", provisional="volt typhoon",
                 subject_ids="lazarus", subject_source="llm")

        # Act / Assert
        assert propose_downgrade_aliases(repo, now=_NOW)["proposed"] == 0

    def test_no_duplicate_across_runs(self, repo: RunHistoryRepository) -> None:
        # Arrange
        _article(repo, "a1", "Kuebiko Phantom の活動", provisional="kuebiko phantom",
                 subject_ids="unc1549", subject_source="llm")
        propose_downgrade_aliases(repo, now=_NOW)

        # Act — 2 回目は dedup_key (news_alias 収穫と同一名前空間) で止まる
        stats = propose_downgrade_aliases(repo, now=_NOW)

        # Assert
        assert stats["proposed"] == 0
        assert stats["skipped"] == 1

    def test_single_word_alias_carries_collision_warning(
        self, repo: RunHistoryRepository
    ) -> None:
        # Arrange — 1 語名は一般語衝突の温床 (2026-07-26 の事故と同型)
        _article(repo, "a1", "Pink による攻撃活動", provisional="pink",
                 subject_ids="lazarus", subject_source="llm")

        # Act
        propose_downgrade_aliases(repo, now=_NOW)

        # Assert
        p = repo.find_actor_update_proposal(
            proposal_type=PROPOSAL_TYPE_NEWS_ALIAS, dedup_key="news_alias:lazarus:pink"
        )
        assert p is not None
        assert "1 語名" in p.rationale

    def test_multi_word_alias_has_no_collision_warning(
        self, repo: RunHistoryRepository
    ) -> None:
        # Arrange
        _article(repo, "a1", "Kuebiko Phantom の活動", provisional="kuebiko phantom",
                 subject_ids="unc1549", subject_source="llm")

        # Act
        propose_downgrade_aliases(repo, now=_NOW)

        # Assert
        p = repo.find_actor_update_proposal(
            proposal_type=PROPOSAL_TYPE_NEWS_ALIAS,
            dedup_key="news_alias:unc1549:kuebiko phantom",
        )
        assert p is not None
        assert "1 語名" not in p.rationale

    def test_dedup_key_uses_normalized_form_not_display(
        self, repo: RunHistoryRepository
    ) -> None:
        # Arrange — 表記揺れ (大小差) で二重起票しないこと
        _article(repo, "a1", "KUEBIKO PHANTOM の活動", provisional="kuebiko phantom",
                 subject_ids="unc1549", subject_source="llm")
        propose_downgrade_aliases(repo, now=_NOW)

        # Act — 別表記の記事が増えても同じ dedup_key に落ちる
        _article(repo, "a2", "Kuebiko Phantom の続報", provisional="kuebiko phantom",
                 subject_ids="unc1549", subject_source="llm")
        stats = propose_downgrade_aliases(repo, now=_NOW)

        # Assert
        assert stats["proposed"] == 0
        assert repo.find_actor_update_proposal(
            proposal_type=PROPOSAL_TYPE_NEWS_ALIAS,
            dedup_key="news_alias:unc1549:kuebiko phantom",
        ) is not None
