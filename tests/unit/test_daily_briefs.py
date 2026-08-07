"""W1 (通知再設計): 日次ブリーフ永続化 (daily_briefs) の round-trip テスト。

段5 weekly_recaps と同パターン。Discord に push される朝刊/夕刊の合成本文を保存し、
Web「日次ブリーフ」ビューで pull 通読できるようにした永続化層を検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.storage.run_history import RunHistoryRepository


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "briefs.db")


class TestDailyBriefPersistence:
    def test_record_and_list_round_trip(self, repo: RunHistoryRepository) -> None:
        repo.record_daily_brief(
            run_id=None,
            slot="morning",
            period_label="2026-06-28",
            title="朝ブリーフィング (2026-06-28)",
            bluf="状況総括 + PIR 3 領域の 24h focus",
            summary="**見出し**\n\n本文セクション…",
            section_count=3,
            sources=[{"title": "JPCERT", "url": "https://example.com/a"}],
            generated_at=datetime(2026, 6, 28, 21, 30, tzinfo=UTC),
        )

        briefs = repo.list_daily_briefs(limit=10)

        assert len(briefs) == 1
        b = briefs[0]
        assert b["slot"] == "morning"
        assert b["period_label"] == "2026-06-28"
        assert b["title"] == "朝ブリーフィング (2026-06-28)"
        assert b["bluf"] == "状況総括 + PIR 3 領域の 24h focus"
        assert b["section_count"] == 3
        assert b["sources"] == [{"title": "JPCERT", "url": "https://example.com/a"}]
        assert "本文セクション" in b["summary"]
        assert b["generated_at"].startswith("2026-06-28")
        assert isinstance(b["id"], int)

    def test_list_orders_newest_first(self, repo: RunHistoryRepository) -> None:
        repo.record_daily_brief(
            run_id=None,
            slot="morning",
            period_label="2026-06-27",
            title="t1",
            bluf="",
            summary="old",
            section_count=0,
            sources=[],
            generated_at=datetime(2026, 6, 27, 21, 30, tzinfo=UTC),
        )
        repo.record_daily_brief(
            run_id=None,
            slot="evening",
            period_label="2026-06-28",
            title="t2",
            bluf="",
            summary="new",
            section_count=0,
            sources=[],
            generated_at=datetime(2026, 6, 28, 10, 30, tzinfo=UTC),
        )

        briefs = repo.list_daily_briefs(limit=10)

        assert [b["summary"] for b in briefs] == ["new", "old"]

    def test_list_respects_limit(self, repo: RunHistoryRepository) -> None:
        for i in range(5):
            repo.record_daily_brief(
                run_id=None,
                slot="morning",
                period_label=f"2026-06-2{i}",
                title="t",
                bluf="",
                summary=f"s{i}",
                section_count=0,
                sources=[],
                generated_at=datetime(2026, 6, 20 + i, 21, 30, tzinfo=UTC),
            )

        assert len(repo.list_daily_briefs(limit=3)) == 3

    def test_empty_returns_empty_list(self, repo: RunHistoryRepository) -> None:
        assert repo.list_daily_briefs() == []

    def test_malformed_sources_degrade_gracefully(self, repo: RunHistoryRepository) -> None:
        # url 欠落 dict が混じっても落とさず、url 有効分のみ返す (boundary 防御)。
        repo.record_daily_brief(
            run_id=None,
            slot="morning",
            period_label="2026-06-28",
            title="t",
            bluf="",
            summary="s",
            section_count=0,
            sources=[{"title": "ok", "url": "https://x"}, {"title": "no-url"}],
            generated_at=datetime(2026, 6, 28, 21, 30, tzinfo=UTC),
        )

        b = repo.list_daily_briefs()[0]

        assert b["sources"] == [{"title": "ok", "url": "https://x"}]


class TestDailyBriefPayload:
    """構造化 payload (2026-07-12): Web 構造描画用 JSON の round-trip。"""

    def test_payload_round_trip(self, repo: RunHistoryRepository) -> None:
        payload = {
            "synthesis": {
                "headline": "見出し",
                "sections": [{"key": "weight_section", "label": "比重", "text": "本文"}],
                "tradecraft": None,
            },
            "pir": [
                {
                    "title": "中国 APT",
                    "total": 4,
                    "summary": "要点",
                    "matches": [
                        {
                            "title": "記事",
                            "url": "https://example.com/x",
                            "importance": "high",
                            "feed": "CISA",
                            "tier": "official",
                        }
                    ],
                }
            ],
        }
        repo.record_daily_brief(
            run_id=None,
            slot="morning",
            period_label="2026-07-12",
            title="朝ブリーフィング (2026-07-12)",
            bluf="",
            summary="全文テキスト",
            section_count=1,
            sources=[],
            payload=payload,
        )

        b = repo.list_daily_briefs(limit=1)[0]
        assert b["payload"] == payload

    def test_old_rows_without_payload_map_to_none(self, repo: RunHistoryRepository) -> None:
        repo.record_daily_brief(
            run_id=None,
            slot="evening",
            period_label="2026-07-11",
            title="夕ブリーフィング (2026-07-11)",
            bluf="",
            summary="旧形式テキスト",
            section_count=0,
            sources=[],
        )

        b = repo.list_daily_briefs(limit=1)[0]
        assert b["payload"] is None
        assert b["summary"] == "旧形式テキスト"  # fallback 表示の原文は維持


class TestMetaOnlyAndById:
    """一覧 meta_only + 単品取得 (2026-07-31 over-fetch 根治: 60件全文~2MB → メタ+選択1件)。"""

    def _record(self, repo: RunHistoryRepository, label: str) -> None:
        repo.record_daily_brief(
            run_id=None,
            slot="morning",
            period_label=label,
            title=f"朝ブリーフィング ({label})",
            bluf="BLUF",
            summary="長い本文 " * 100,
            section_count=2,
            sources=[{"title": "src", "url": "https://example.com"}],
            generated_at=datetime(2026, 7, 1, 21, 30, tzinfo=UTC),
        )

    def test_meta_only_excludes_heavy_fields(self, repo: RunHistoryRepository) -> None:
        self._record(repo, "2026-07-01")
        metas = repo.list_daily_briefs(limit=10, meta_only=True)
        assert len(metas) == 1
        m = metas[0]
        # 一覧サイドバーに必要なメタは揃う
        assert m["slot"] == "morning"
        assert m["period_label"] == "2026-07-01"
        assert m["title"].startswith("朝ブリーフィング")
        assert m["section_count"] == 2
        assert m["generated_at"].startswith("2026-07-01")
        # heavy 列 (本文/構造 payload/出典) は含まれない = over-fetch しない
        for heavy in ("summary", "payload", "bluf", "sources"):
            assert heavy not in m

    def test_get_daily_brief_returns_full(self, repo: RunHistoryRepository) -> None:
        self._record(repo, "2026-07-02")
        brief_id = repo.list_daily_briefs(limit=1, meta_only=True)[0]["id"]
        full = repo.get_daily_brief(int(brief_id))
        assert full is not None
        assert "長い本文" in full["summary"]
        assert full["bluf"] == "BLUF"
        assert full["sources"] == [{"title": "src", "url": "https://example.com"}]

    def test_get_daily_brief_missing_returns_none(self, repo: RunHistoryRepository) -> None:
        assert repo.get_daily_brief(999999) is None
