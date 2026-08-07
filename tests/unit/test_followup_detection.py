"""Phase B-续报: Discord 続報 marker のテスト。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.cti.followup_detection import annotate_followup
from src.storage.run_history import (
    ArticleRecord,
    RunHistoryRepository,
    RunRecord,
)
from src.tools.discord_publisher import BriefingMessage


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "f.db")


def _now() -> datetime:
    return datetime.now(UTC)


def _make_msg(dedup_key: str = "shai-hulud-npm") -> BriefingMessage:
    return BriefingMessage(
        title="新 Shai-Hulud 拡大",
        bluf="b",
        importance="high",
        category="vulnerability",
        summary="s",
        metadata={"dedup_key": dedup_key},
    )


class TestAnnotateFollowup:
    def test_returns_unchanged_when_no_prior(self, repo: RunHistoryRepository) -> None:
        msg = _make_msg()
        out = annotate_followup(msg, repo=repo)
        assert "followup_info" not in out.metadata
        assert out.title == msg.title

    def test_returns_unchanged_when_no_dedup_key(self, repo: RunHistoryRepository) -> None:
        msg = BriefingMessage(
            title="t",
            bluf="b",
            importance="high",
            category="vulnerability",
            summary="s",
            metadata={},
        )
        out = annotate_followup(msg, repo=repo)
        assert "followup_info" not in out.metadata

    def test_annotates_when_prior_post_exists(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        # 3 日前に同 dedup_key で post された article を seed
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="x", dry_run=False),
        )
        prior_at = _now() - timedelta(days=3)
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="prev1",
                title="初報 Shai-Hulud",
                url="https://example.com/1",
                status="posted",
                dedup_key="shai-hulud-npm",
                created_at=prior_at,
            ),
        )
        msg = _make_msg()
        out = annotate_followup(msg, repo=repo)
        fi = out.metadata["followup_info"]
        assert isinstance(fi, dict)
        assert fi["prior_count"] == 1
        assert "初報 Shai-Hulud" in str(fi["prior_title"])

    def test_counts_multiple_priors(self, repo: RunHistoryRepository) -> None:
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="x", dry_run=False),
        )
        for i, days in enumerate([5, 3, 2]):
            repo.add_article(
                ArticleRecord(
                    run_id=run_id,
                    article_id=f"prev{i}",
                    title=f"prev {i}",
                    url=f"https://example.com/{i}",
                    status="posted",
                    dedup_key="teampcp-supply-chain",
                    created_at=_now() - timedelta(days=days),
                ),
            )
        msg = BriefingMessage(
            title="TeamPCP 新展開",
            bluf="b",
            importance="high",
            category="vulnerability",
            summary="s",
            metadata={"dedup_key": "teampcp-supply-chain"},
        )
        out = annotate_followup(msg, repo=repo)
        assert out.metadata["followup_info"]["prior_count"] == 3

    def test_lookback_excludes_old_posts(self, repo: RunHistoryRepository) -> None:
        """lookback_hours より古い post は対象外。"""
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="x", dry_run=False),
        )
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="ancient",
                title="超古い",
                url="https://example.com/x",
                status="posted",
                dedup_key="old-campaign",
                created_at=_now() - timedelta(days=30),
            ),
        )
        msg = BriefingMessage(
            title="新発見",
            bluf="b",
            importance="high",
            category="vulnerability",
            summary="s",
            metadata={"dedup_key": "old-campaign"},
        )
        # 168h = 7 日 → 30 日前は除外される
        out = annotate_followup(msg, repo=repo, lookback_hours=168)
        assert "followup_info" not in out.metadata


class TestSemanticFollowup:
    """Phase B-cal: dedup_key 不一致でも embedding 類似で続報検出。"""

    def test_semantic_match_annotates(self, repo: RunHistoryRepository) -> None:
        # 2 日前に投稿された記事の seen 記録 → embedding を seed
        # (article_embeddings.url_hash は dedup_seen_urls への FK なので seen が先)
        prior_at = _now() - timedelta(days=2)
        repo.mark_url_seen(
            url_hash="hprev",
            url="https://example.com/prev",
            title="初報: ある breach",
            when=prior_at,
        )
        repo.add_article_embedding(
            url_hash="hprev",
            url="https://example.com/prev",
            vector=[1.0, 0.0, 0.0],
            model="emb-m",
            title="初報: ある breach",
            when=prior_at,
        )
        # dedup_key が異なる (LLM slug 変動を模擬) が embedding は酷似
        msg = BriefingMessage(
            title="続報: 同じ breach の新展開",
            bluf="b",
            importance="high",
            category="breach",
            summary="s",
            metadata={"dedup_key": "totally-different-slug"},
        )
        out = annotate_followup(
            msg,
            repo=repo,
            embedding=[0.99, 0.01, 0.0],
            embedding_model="emb-m",
        )
        fi = out.metadata["followup_info"]
        assert fi["via"] == "semantic"
        assert fi["prior_count"] == 1
        assert "初報" in str(fi["prior_title"])
        assert fi["similarity"] >= 0.88

    def test_semantic_below_threshold_no_match(self, repo: RunHistoryRepository) -> None:
        prior_at = _now() - timedelta(days=2)
        repo.mark_url_seen(
            url_hash="hprev",
            url="https://example.com/prev",
            title="prev",
            when=prior_at,
        )
        repo.add_article_embedding(
            url_hash="hprev",
            url="https://example.com/prev",
            vector=[1.0, 0.0, 0.0],
            model="emb-m",
            when=prior_at,
        )
        msg = _make_msg(dedup_key="")  # dedup_key なし → semantic のみ
        out = annotate_followup(
            msg,
            repo=repo,
            embedding=[0.0, 1.0, 0.0],
            embedding_model="emb-m",
        )  # 直交ベクトル = sim 0 → 閾値未満
        assert "followup_info" not in out.metadata


class TestDiscordTitlePrefix:
    """続報 marker が Discord の embed title に prefix される。"""

    def test_apply_followup_prefix_adds_marker(self) -> None:
        from src.tools.discord_publisher import _apply_followup_prefix

        msg = BriefingMessage(
            title="新展開",
            bluf="b",
            importance="high",
            category="vulnerability",
            summary="s",
            metadata={
                "followup_info": {
                    "prior_count": 2,
                    "prior_at_iso": "2026-05-18T00:00:00+00:00",
                    "prior_title": "初報",
                },
            },
        )
        out = _apply_followup_prefix(msg)
        assert out.startswith("🔁 [続報]")
        assert "新展開" in out

    def test_apply_followup_prefix_no_marker_when_absent(self) -> None:
        from src.tools.discord_publisher import _apply_followup_prefix

        msg = BriefingMessage(
            title="通常 post",
            bluf="b",
            importance="high",
            category="vulnerability",
            summary="s",
            metadata={},
        )
        assert _apply_followup_prefix(msg) == "通常 post"
