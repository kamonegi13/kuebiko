"""Phase 2 K6: synthesis / spotlight の Discord 配信 (brief / watch) のテスト。

posted_at による再配信 dedup と BriefingMessage 整形を検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from src.spotlight.discord import build_spotlight_message, deliver_spotlights
from src.spotlight.models import KeyEvent, SpotlightRecord
from src.storage.run_history import (
    RunHistoryRepository,
    StatusSynthesisRecord,
)
from src.synthesis.discord import build_synthesis_message, deliver_synthesis
from src.tools.discord_publisher import BriefingMessage, DiscordPublisher


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "k6.db")


class _FakePublisher:
    """post を記録するだけの publisher (DiscordPublisher と duck-typing 互換)。"""

    def __init__(self) -> None:
        self.posted: list[BriefingMessage] = []

    async def post(self, message: BriefingMessage, **_: object) -> None:
        self.posted.append(message)


class _FailingPublisher:
    async def post(self, message: BriefingMessage, **_: object) -> None:
        raise RuntimeError("discord down")


# period_start は production の _resolve_period 同様 period 境界に固定する
# (再 UPSERT が同一行を更新するように。now() だと毎回別 period 扱いになる)。
_FIXED_START = datetime(2026, 6, 1, tzinfo=UTC)


def _synthesis(period_type: str = "weekly") -> StatusSynthesisRecord:
    return StatusSynthesisRecord(
        period_type=period_type,
        period_start=_FIXED_START,
        period_end=_FIXED_START,
        headline="今週は中国APTの通信事業者標的が比重を増した",
        weight_section="軍事/情報軸が突出",
        chain_section="脆弱性公開→悪用の連鎖",
        cog_section="重心はエッジ機器",
        spillover_section="周辺国にも波及",
        pir_section="PIR-中国: 達成度高",
        axes_evidence="{}",
        article_count=42,
        llm_model="gemma4:31b",
    )


def _spotlight(pir_id: str = "pir_china_apt") -> SpotlightRecord:
    return SpotlightRecord(
        pir_id=pir_id,
        pir_title="中国 APT 動向",
        period_type="weekly",
        period_start=_FIXED_START,
        period_end=_FIXED_START,
        headline="Volt Typhoon の事前配置が継続",
        outlook="来週はエッジ機器の新規 CVE に警戒",
        key_events=[
            KeyEvent(article_id="a1", title="記事1", url="https://e/1"),
            KeyEvent(article_id="a2", title="記事2", url="https://e/2"),
            KeyEvent(article_id="a3", title="URLなし", url=""),
        ],
        article_count=7,
        llm_model="gemma4:26b",
    )


class TestBuildMessages:
    def test_synthesis_message_includes_headline_and_sections(self) -> None:
        msg = build_synthesis_message(_synthesis("weekly"))
        assert "状況総括 (週次)" in msg.title
        assert msg.importance == "medium"
        assert msg.category == "status_synthesis"
        assert "今週は中国APT" in msg.summary
        assert "比重" in msg.summary  # section header
        assert "PIR 達成度" in msg.summary

    def test_synthesis_message_includes_tradecraft_when_present(self) -> None:
        """S2: tradecraft があれば対立仮説等が Discord 本文に載る。"""
        import json as _json

        rec = _synthesis("weekly")
        tc = _json.dumps(
            {
                "leading_assessment": "中東緊張がサイバー作戦を駆動",
                "alternatives": ["金銭目的犯罪がAPTを模倣した可能性"],
                "key_assumptions": ["attribution報告が正確"],
                "indicators": ["同一TTPが別地域で観測されれば広域作戦と確定"],
            },
            ensure_ascii=False,
        )
        msg = build_synthesis_message(rec.model_copy(update={"tradecraft": tc}))
        assert "分析トレードクラフト" in msg.summary
        assert "対立仮説" in msg.summary
        assert "金銭目的犯罪がAPTを模倣" in msg.summary

    def test_synthesis_message_omits_tradecraft_when_empty(self) -> None:
        msg = build_synthesis_message(_synthesis("weekly"))  # tradecraft="" (default)
        assert "分析トレードクラフト" not in msg.summary

    def test_spotlight_message_uses_key_events_as_sources(self) -> None:
        msg = build_spotlight_message(_spotlight())
        assert "中国 APT 動向" in msg.title
        assert "Volt Typhoon" in msg.summary
        assert "来週はエッジ機器" in msg.summary
        # URL ありの 2 件のみ sources に載る (空 URL は除外)
        assert [s.url for s in msg.sources] == ["https://e/1", "https://e/2"]


class TestSynthesisPostedAtRoundtrip:
    def test_mark_posted_sets_posted_at(self, repo: RunHistoryRepository) -> None:
        repo.upsert_status_synthesis(_synthesis("weekly"))
        rec = repo.get_latest_synthesis(period_type="weekly")
        assert rec is not None and rec.posted_at is None
        repo.mark_synthesis_posted(period_type="weekly", period_start=rec.period_start)
        rec2 = repo.get_latest_synthesis(period_type="weekly")
        assert rec2 is not None and rec2.posted_at is not None

    def test_upsert_preserves_posted_at(self, repo: RunHistoryRepository) -> None:
        """再生成 (UPSERT) では posted_at を消さない (再配信 dedup の要)。"""
        repo.upsert_status_synthesis(_synthesis("weekly"))
        rec = repo.get_latest_synthesis(period_type="weekly")
        assert rec is not None
        repo.mark_synthesis_posted(period_type="weekly", period_start=rec.period_start)
        # 同 period を再 UPSERT (内容更新)
        repo.upsert_status_synthesis(_synthesis("weekly"))
        rec2 = repo.get_latest_synthesis(period_type="weekly")
        assert rec2 is not None and rec2.posted_at is not None


class TestDeliverSynthesis:
    @pytest.mark.asyncio
    async def test_delivers_unposted_and_marks(self, repo: RunHistoryRepository) -> None:
        repo.upsert_status_synthesis(_synthesis("weekly"))
        pub = _FakePublisher()
        delivered = await deliver_synthesis(
            repo=repo,
            publisher=cast(DiscordPublisher, pub),
            period_types=["weekly"],
        )
        assert delivered == ["weekly"]
        assert len(pub.posted) == 1
        # 2 回目は posted_at が立っているので skip
        delivered2 = await deliver_synthesis(
            repo=repo,
            publisher=cast(DiscordPublisher, pub),
            period_types=["weekly"],
        )
        assert delivered2 == []
        assert len(pub.posted) == 1

    @pytest.mark.asyncio
    async def test_failure_does_not_mark_posted(self, repo: RunHistoryRepository) -> None:
        repo.upsert_status_synthesis(_synthesis("weekly"))
        delivered = await deliver_synthesis(
            repo=repo,
            publisher=cast(DiscordPublisher, _FailingPublisher()),
            period_types=["weekly"],
        )
        assert delivered == []
        # 投稿失敗なら posted_at は据え置き → 次回 run でリトライできる
        rec = repo.get_latest_synthesis(period_type="weekly")
        assert rec is not None and rec.posted_at is None

    @pytest.mark.asyncio
    async def test_missing_period_skipped(self, repo: RunHistoryRepository) -> None:
        pub = _FakePublisher()
        delivered = await deliver_synthesis(
            repo=repo,
            publisher=cast(DiscordPublisher, pub),
            period_types=["weekly"],
        )
        assert delivered == []
        assert pub.posted == []


class TestDeliverSpotlights:
    @pytest.mark.asyncio
    async def test_delivers_unposted_and_dedups(self, repo: RunHistoryRepository) -> None:
        repo.upsert_pir_spotlight(_spotlight("pir_china_apt"))
        pub = _FakePublisher()
        delivered = await deliver_spotlights(
            repo=repo,
            publisher=pub,  # type: ignore[arg-type]
            pir_ids=["pir_china_apt"],
            period_type="weekly",
        )
        assert delivered == ["pir_china_apt"]
        assert len(pub.posted) == 1
        # 再配信 dedup
        delivered2 = await deliver_spotlights(
            repo=repo,
            publisher=pub,  # type: ignore[arg-type]
            pir_ids=["pir_china_apt"],
            period_type="weekly",
        )
        assert delivered2 == []
        assert len(pub.posted) == 1

    @pytest.mark.asyncio
    async def test_failure_keeps_unposted(self, repo: RunHistoryRepository) -> None:
        repo.upsert_pir_spotlight(_spotlight("pir_china_apt"))
        delivered = await deliver_spotlights(
            repo=repo,
            publisher=_FailingPublisher(),  # type: ignore[arg-type]
            pir_ids=["pir_china_apt"],
            period_type="weekly",
        )
        assert delivered == []
        rec = repo.get_latest_spotlight(pir_id="pir_china_apt", period_type="weekly")
        assert rec is not None and rec.posted_at is None


class TestBuildSynthesisCompact:
    """Discord 要点射影 (2026-07-12): headline + 比重 + 重心のみ。全文は Web で読む。"""

    def test_includes_headline_weight_and_cog_only(self) -> None:
        from src.synthesis.discord import build_synthesis_compact

        text = build_synthesis_compact(_synthesis("daily"))
        assert text.startswith("**今週は中国APTの通信事業者標的が比重を増した**")
        assert "■ 比重" in text and "軍事/情報軸が突出" in text
        assert "■ 重心" in text and "重心はエッジ機器" in text
        # 全文 push を構成していた節は含めない (Web 全文で読む)
        assert "連鎖" not in text
        assert "波及" not in text
        assert "PIR 達成度" not in text
        assert "トレードクラフト" not in text

    def test_skips_empty_sections(self) -> None:
        from src.synthesis.discord import build_synthesis_compact

        rec = _synthesis("daily").model_copy(update={"weight_section": "", "cog_section": "  "})
        text = build_synthesis_compact(rec)
        assert text == "**今週は中国APTの通信事業者標的が比重を増した**"
