"""src.cti.alert_demotion のテスト (R1b: 2 関門の統合)。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.cti.alert_dedup_gate import GATE_ENV as DEDUP_GATE_ENV
from src.cti.alert_dedup_gate import AlertDedupMatch
from src.cti.alert_demotion import (
    DEMOTED_CHANNEL,
    HIGH_CONFIDENCE_BYPASS_THRESHOLD,
    AlertDemotionResult,
    _should_suppress_duplicate,
    maybe_demote_alert,
)
from src.cti.law_enforcement_gate import GATE_ENV as LAW_GATE_ENV
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord
from src.tools.discord_publisher import BriefingMessage


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "demotion.db")


def _now() -> datetime:
    return datetime.now(UTC)


def _make_msg(
    title: str = "t",
    summary: str = "s",
    dedup_key: str = "",
    detected_actor_ids: list[str] | None = None,
    victim_orgs: list[str] | None = None,
) -> BriefingMessage:
    metadata: dict[str, object] = {}
    if dedup_key:
        metadata["dedup_key"] = dedup_key
    if detected_actor_ids:
        metadata["detected_actor_ids"] = detected_actor_ids
    if victim_orgs:
        metadata["victim_orgs"] = victim_orgs
    return BriefingMessage(
        title=title,
        bluf="",
        importance="high",
        category="apt",
        summary=summary,
        metadata=metadata,
    )


async def _demote(
    msg: BriefingMessage,
    *,
    repo: RunHistoryRepository,
    article_id: str = "a2",
) -> AlertDemotionResult | None:
    return await maybe_demote_alert(
        msg,
        article_url=f"https://example.com/{article_id}",
        article_id=article_id,
        category="apt",
        feed_title="feed",
        published_at=_now(),
        embedding=None,
        config=object(),  # type: ignore[arg-type]
        dedup_repo=repo,
    )


class TestMaybeDemoteAlert:
    @pytest.mark.asyncio
    async def test_returns_none_when_both_gates_disabled(
        self, monkeypatch: pytest.MonkeyPatch, repo: RunHistoryRepository
    ) -> None:
        monkeypatch.delenv(LAW_GATE_ENV, raising=False)
        monkeypatch.delenv(DEDUP_GATE_ENV, raising=False)
        msg = _make_msg(title="FBI、ハッキング基盤を無力化")
        result = await _demote(msg, repo=repo)
        assert result is None

    @pytest.mark.asyncio
    async def test_law_enforcement_gate_demotes_on_keyword(
        self, monkeypatch: pytest.MonkeyPatch, repo: RunHistoryRepository
    ) -> None:
        monkeypatch.setenv(LAW_GATE_ENV, "1")
        monkeypatch.delenv(DEDUP_GATE_ENV, raising=False)

        msg = _make_msg(title="FBI、中国系ハッキング基盤を無力化")
        result = await _demote(msg, repo=repo)
        assert result is not None
        assert result.reason == "law_enforcement_announcement"

    @pytest.mark.asyncio
    async def test_law_enforcement_gate_skips_when_not_announcement(
        self, monkeypatch: pytest.MonkeyPatch, repo: RunHistoryRepository
    ) -> None:
        monkeypatch.setenv(LAW_GATE_ENV, "1")
        monkeypatch.delenv(DEDUP_GATE_ENV, raising=False)

        msg = _make_msg(title="新しいゼロデイが広く悪用中")
        result = await _demote(msg, repo=repo)
        assert result is None

    @pytest.mark.asyncio
    async def test_dedup_gate_demotes_when_prior_alert_matches_and_no_new_entities(
        self, monkeypatch: pytest.MonkeyPatch, repo: RunHistoryRepository
    ) -> None:
        monkeypatch.delenv(LAW_GATE_ENV, raising=False)
        monkeypatch.setenv(DEDUP_GATE_ENV, "1")
        run_id = repo.start_run(RunRecord(started_at=_now(), pipeline="x", dry_run=False))
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="a1",
                title="同一事象の先行 alert",
                url="https://example.com/a1",
                feed_title="feed",
                status="posted",
                posted_channel="alert",
                dedup_key="same-event",
                created_at=_now() - timedelta(hours=1),
            ),
        )
        repo.add_article_entities("a1", [("actor", "flax-typhoon")])
        msg = _make_msg(
            title="続報 (内容同一)", dedup_key="same-event", detected_actor_ids=["flax-typhoon"]
        )
        result = await _demote(msg, repo=repo)
        assert result is not None
        assert result.reason == "alert_dedup_event:dedup_key"

    @pytest.mark.asyncio
    async def test_dedup_key_exact_match_suppresses_even_with_new_entity(
        self, monkeypatch: pytest.MonkeyPatch, repo: RunHistoryRepository
    ) -> None:
        """dedup_key 完全一致は高確信 (bypass 相当) として扱い、entity-diff を経ずに抑止する
        (2026-10-11 コーディネータ指示の backtest で校正、`_should_suppress_duplicate` 参照)。
        entity-diff が効くのは pair_model 一致 かつ 確率が bypass 閾値未満のときだけ
        (``TestShouldSuppressDuplicate`` で直接検証)。
        """
        monkeypatch.delenv(LAW_GATE_ENV, raising=False)
        monkeypatch.setenv(DEDUP_GATE_ENV, "1")
        run_id = repo.start_run(RunRecord(started_at=_now(), pipeline="x", dry_run=False))
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="a1",
                title="同一事象の先行 alert",
                url="https://example.com/a1",
                feed_title="feed",
                status="posted",
                posted_channel="alert",
                dedup_key="same-event",
                created_at=_now() - timedelta(hours=1),
            ),
        )
        repo.add_article_entities("a1", [("actor", "flax-typhoon")])
        msg = _make_msg(
            title="続報 (新しい被害組織が判明)",
            dedup_key="same-event",
            detected_actor_ids=["flax-typhoon"],
            victim_orgs=["Acme Corp"],  # 先行記事には無い新しい固有名詞
        )
        result = await _demote(msg, repo=repo)
        assert result is not None
        assert result.reason == "alert_dedup_event:dedup_key"

    @pytest.mark.asyncio
    async def test_dedup_gate_disabled_returns_none_even_with_prior_match(
        self, monkeypatch: pytest.MonkeyPatch, repo: RunHistoryRepository
    ) -> None:
        monkeypatch.delenv(LAW_GATE_ENV, raising=False)
        monkeypatch.delenv(DEDUP_GATE_ENV, raising=False)
        run_id = repo.start_run(RunRecord(started_at=_now(), pipeline="x", dry_run=False))
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="a1",
                title="同一事象の先行 alert",
                url="https://example.com/a1",
                feed_title="feed",
                status="posted",
                posted_channel="alert",
                dedup_key="same-event",
                created_at=_now() - timedelta(hours=1),
            ),
        )
        msg = _make_msg(title="続報 (内容同一)", dedup_key="same-event")
        result = await _demote(msg, repo=repo)
        assert result is None


def test_demoted_channel_is_watch() -> None:
    assert DEMOTED_CHANNEL == "watch"


class TestShouldSuppressDuplicate:
    """高確信 bypass (R1b 続き、2026-10-11) の単体テスト。"""

    def test_exact_match_always_suppresses(self) -> None:
        match = AlertDedupMatch("a1", "t", "dedup_key", frozenset(), probability=0.0)
        current = frozenset({("victim_org", "新しい被害企業")})  # 新事実があっても
        assert _should_suppress_duplicate(match, current) is True

    def test_pair_model_above_threshold_bypasses_entity_diff(self) -> None:
        match = AlertDedupMatch(
            "a1",
            "t",
            "pair_model",
            frozenset({("actor", "flax-typhoon")}),
            probability=HIGH_CONFIDENCE_BYPASS_THRESHOLD,
        )
        current = frozenset({("actor", "flax-typhoon"), ("victim_org", "新しい被害企業")})
        assert _should_suppress_duplicate(match, current) is True

    def test_pair_model_below_threshold_with_new_entity_keeps_alert(self) -> None:
        match = AlertDedupMatch(
            "a1",
            "t",
            "pair_model",
            frozenset({("actor", "flax-typhoon")}),
            probability=HIGH_CONFIDENCE_BYPASS_THRESHOLD - 0.01,
        )
        current = frozenset({("actor", "flax-typhoon"), ("victim_org", "新しい被害企業")})
        assert _should_suppress_duplicate(match, current) is False

    def test_pair_model_below_threshold_without_new_entity_suppresses(self) -> None:
        match = AlertDedupMatch(
            "a1",
            "t",
            "pair_model",
            frozenset({("actor", "flax-typhoon")}),
            probability=0.5,
        )
        current = frozenset({("actor", "flax-typhoon")})
        assert _should_suppress_duplicate(match, current) is True
