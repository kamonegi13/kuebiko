"""japan_relevance SSoT (有機的結合監査 H4) と要対応 KPI 拡張 (H5) の unit test。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.cti.japan_relevance import (
    JAPAN_TARGETED_SQL_FRAGMENT,
    is_japan_targeted_row,
    japan_claim_relevance,
)
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord


class TestIsJapanTargetedRow:
    def test_victim_jp(self) -> None:
        assert is_japan_targeted_row("JP", "alert") is True

    def test_channel_japan_watch(self) -> None:
        # victim 抽出漏れでも japan_watch 配信なら日本標的 (threat_ops の旧 channel-only
        # 方言と dashboard の victim+channel 方言を統一した定義)
        assert is_japan_targeted_row(None, "japan_watch") is True

    def test_lowercase_iso_normalized(self) -> None:
        assert is_japan_targeted_row("jp", "watch") is True

    def test_neither(self) -> None:
        assert is_japan_targeted_row("US", "alert") is False
        assert is_japan_targeted_row(None, None) is False


class TestJapanClaimRelevance:
    def test_claim_text_mentions_japan_is_party(self) -> None:
        assert japan_claim_relevance("日本の重要インフラへの攻撃", set()) == "party"

    def test_involved_country_is_party(self) -> None:
        assert japan_claim_relevance("attack on infra", {"involved_country:JP"}) == "party"

    def test_mentioned_country_only_is_mention(self) -> None:
        # H4 の核心: 言及のみは routing が排除する層 → salience でも下駄を履かせない
        assert japan_claim_relevance("global campaign", {"mentioned_country:JP"}) == "mention"

    def test_none(self) -> None:
        assert japan_claim_relevance("US grid attack", {"involved_country:US"}) == "none"


class TestActionableCount:
    """要対応 KPI (H5 拡張): JP ランサム被害・JP prepositioning が high でなくても入る。"""

    @pytest.fixture
    def repo(self, tmp_path: Path) -> RunHistoryRepository:
        return RunHistoryRepository(db_path=tmp_path / "kpi.db")

    def _add(
        self,
        repo: RunHistoryRepository,
        *,
        article_id: str,
        importance: str = "medium",
        victim_iso: str | None = None,
        channel: str = "watch",
        is_ransomware: bool = False,
        intent: str | None = None,
        intent_confidence: str | None = None,
    ) -> None:
        run_id = repo.start_run(
            RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False)
        )
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id=article_id,
                title=f"t-{article_id}",
                url=f"https://example.com/{article_id}",
                feed_title="f",
                summary="s",
                importance=importance,
                posted_channel=channel,
                status="posted",
                category="incident",
                victim_country_iso=victim_iso,
                is_ransomware=is_ransomware,
                socio_political_intent=intent,
                intent_confidence=intent_confidence,
                created_at=datetime.now(UTC) - timedelta(hours=1),
            ),
        )

    def _count(self, repo: RunHistoryRepository) -> int:
        from src.storage.db_backend import connect
        from src.ui.services.overview import _actionable_count

        con = connect(repo.db_path)
        con.row_factory = __import__("sqlite3").Row
        try:
            return _actionable_count(
                con,
                datetime.now(UTC) - timedelta(days=1),
                datetime.now(UTC) + timedelta(days=1),
                frozenset(),
            )
        finally:
            con.close()

    def test_jp_ransomware_medium_counts(self, repo: RunHistoryRepository) -> None:
        # Arrange: ransomware 取込は medium 固定 (H5 の構造的漏れの再現)
        self._add(
            repo,
            article_id="r1",
            importance="medium",
            victim_iso="JP",
            channel="japan_watch",
            is_ransomware=True,
        )

        # Act / Assert
        assert self._count(repo) == 1

    def test_jp_prepositioning_confirmed_counts(self, repo: RunHistoryRepository) -> None:
        self._add(
            repo,
            article_id="p1",
            importance="medium",
            victim_iso="JP",
            intent="prepositioning",
            intent_confidence="medium",
        )

        assert self._count(repo) == 1

    def test_jp_prepositioning_low_hypothesis_excluded(self, repo: RunHistoryRepository) -> None:
        # low=仮説は要対応に昇格させない (H3 の確度配線と整合)
        self._add(
            repo,
            article_id="p2",
            importance="medium",
            victim_iso="JP",
            intent="prepositioning",
            intent_confidence="low",
        )

        assert self._count(repo) == 0

    def test_high_jp_still_counts(self, repo: RunHistoryRepository) -> None:
        self._add(repo, article_id="h1", importance="high", victim_iso="JP")

        assert self._count(repo) == 1

    def test_non_jp_medium_ransomware_excluded(self, repo: RunHistoryRepository) -> None:
        self._add(
            repo,
            article_id="r2",
            importance="medium",
            victim_iso="US",
            is_ransomware=True,
        )

        assert self._count(repo) == 0


class TestSqlFragment:
    def test_fragment_matches_row_predicate(self, tmp_path: Path) -> None:
        """SQL fragment と Python 述語が同じ判定を返す (SSoT の等価性)。"""
        repo = RunHistoryRepository(db_path=tmp_path / "frag.db")
        run_id = repo.start_run(
            RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False)
        )
        cases = [
            ("a1", "JP", "alert"),
            ("a2", None, "japan_watch"),
            ("a3", "US", "watch"),
        ]
        for aid, iso, ch in cases:
            repo.add_article(
                ArticleRecord(
                    run_id=run_id,
                    article_id=aid,
                    title=aid,
                    url=f"https://example.com/{aid}",
                    feed_title="f",
                    summary="s",
                    importance="medium",
                    posted_channel=ch,
                    status="posted",
                    victim_country_iso=iso,
                    created_at=datetime.now(UTC),
                ),
            )
        from src.storage.db_backend import connect

        con = connect(repo.db_path)
        try:
            rows = con.execute(
                "SELECT article_id, victim_country_iso, posted_channel FROM articles "
                f"WHERE {JAPAN_TARGETED_SQL_FRAGMENT}"
            ).fetchall()
        finally:
            con.close()
        sql_ids = {str(r[0]) for r in rows}
        py_ids = {aid for aid, iso, ch in cases if is_japan_targeted_row(iso, ch)}
        assert sql_ids == py_ids == {"a1", "a2"}
