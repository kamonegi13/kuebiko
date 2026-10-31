"""soft deadline の予備秒 (2026-10-31: 300 → 150) と、着手済み記事の timeout の頭打ち。"""

from __future__ import annotations

import asyncio
import time

import pytest

from src.pipeline import orchestrator as orch

_BUDGET = 705.0


def test_default_reserve_leaves_processing_budget_of_555s(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(orch._DEADLINE_RESERVE_ENV, raising=False)
    before = time.monotonic()
    deadline = orch._compute_soft_deadline(_BUDGET)
    assert deadline is not None
    assert deadline - before == pytest.approx(555.0, abs=1.0)


def test_env_override_restores_old_reserve(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(orch._DEADLINE_RESERVE_ENV, "300")
    before = time.monotonic()
    deadline = orch._compute_soft_deadline(_BUDGET)
    assert deadline is not None
    assert deadline - before == pytest.approx(405.0, abs=1.0)


@pytest.mark.parametrize("raw", ["abc", "-5"])
def test_invalid_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(orch._DEADLINE_RESERVE_ENV, raw)
    assert orch._deadline_reserve_seconds() == orch._DEADLINE_RESERVE_SECONDS


def test_floor_ratio_keeps_half_budget_for_short_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(orch._DEADLINE_RESERVE_ENV, raising=False)
    before = time.monotonic()
    deadline = orch._compute_soft_deadline(200.0)  # 200-150=50 < 100 -> 下限 100
    assert deadline is not None
    assert deadline - before == pytest.approx(100.0, abs=1.0)


def test_no_budget_means_no_deadline() -> None:
    assert orch._compute_soft_deadline(None) is None
    assert orch._compute_soft_deadline(0) is None


def test_article_timeout_capped_by_remaining_budget() -> None:
    # 残り 150 秒 -> 150-90=60 秒で頭打ち (base 600 より短い)
    assert orch._article_timeout_within_budget(600.0, 150.0) == 60.0
    # 残りが十分ならそのまま
    assert orch._article_timeout_within_budget(600.0, 5000.0) == 600.0
    # 残りがほぼ無くても下限は守る
    assert orch._article_timeout_within_budget(600.0, 10.0) == orch._MIN_ARTICLE_TIMEOUT_SECONDS
    # 予算未指定 / base 無効
    assert orch._article_timeout_within_budget(600.0, None) == 600.0
    assert orch._article_timeout_within_budget(None, 150.0) == 60.0
    assert orch._article_timeout_within_budget(None, None) is None


@pytest.mark.asyncio
async def test_in_flight_article_finishes_before_hard_limit() -> None:
    """時間を 1/100 に縮めた模擬: soft deadline 直前に着手した暴走記事も hard limit 内で止まる。"""
    scale = 0.01
    hard_limit = _BUDGET * scale
    start = time.monotonic()
    soft_at = (_BUDGET - orch._DEADLINE_RESERVE_SECONDS) * scale
    # soft deadline 直前に着手 -> 残りは hard_limit - soft_at
    remaining = (start + hard_limit) - (start + soft_at)
    timeout = orch._article_timeout_within_budget(600.0 * scale, remaining / scale)
    assert timeout is not None

    async def runaway() -> None:
        await asyncio.sleep(10)

    began = time.monotonic()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(runaway(), timeout=timeout * scale)
    finished_at = (began - start) + (time.monotonic() - began)
    post_loop = orch._POST_LOOP_RESERVE_SECONDS * scale
    assert soft_at + finished_at <= hard_limit - post_loop + 0.05


@pytest.mark.parametrize("source_type", ["grok_email", "web_scraper_cluster"])
def test_non_rss_sources_keep_legacy_300s_reserve(
    monkeypatch: pytest.MonkeyPatch, source_type: str
) -> None:
    # env で予備を縮めても rss 以外は 300 秒のまま (Grok は 1 投稿約 230 秒・投稿単位の timeout 無し)
    monkeypatch.setenv(orch._DEADLINE_RESERVE_ENV, "150")
    before = time.monotonic()
    deadline = orch._compute_soft_deadline(1185.0, source_type)
    assert deadline is not None
    assert deadline - before == pytest.approx(885.0, abs=1.0)


def test_rss_source_uses_short_reserve(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(orch._DEADLINE_RESERVE_ENV, raising=False)
    before = time.monotonic()
    deadline = orch._compute_soft_deadline(705.0, "rss")
    assert deadline is not None
    assert deadline - before == pytest.approx(555.0, abs=1.0)


def test_budget_cap_flag_off_keeps_base_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(orch._ARTICLE_TIMEOUT_BUDGET_CAP_ENV, "0")
    assert orch._article_timeout_within_budget(600.0, 150.0) == 600.0
    monkeypatch.setenv(orch._ARTICLE_TIMEOUT_BUDGET_CAP_ENV, "1")
    assert orch._article_timeout_within_budget(600.0, 150.0) == 60.0
