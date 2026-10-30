"""model_tiers の DB 読み込み失敗の警報 (TIER-LOAD-ALARM)。"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from src.tools import model_tiers, tier_load_alarm
from src.tools.model_tiers import BUILTIN_MODEL_TIERS, Tier, resolve_tier_model


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    model_tiers._CACHE.clear()
    model_tiers._FAILED_AT.clear()
    tier_load_alarm.reset_for_tests()
    yield
    model_tiers._CACHE.clear()
    model_tiers._FAILED_AT.clear()
    tier_load_alarm.reset_for_tests()


def test_load_failure_falls_back_and_counts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("attempt to write a readonly database")

    monkeypatch.setattr("src.storage.config_store.get_config", boom)

    model = resolve_tier_model(Tier.FAST, db_path=tmp_path / "x.db")

    assert model == BUILTIN_MODEL_TIERS[Tier.FAST.value]
    state = tier_load_alarm.tier_load_failure_state()
    assert state["count"] == 1
    assert state["last_error_type"] == "RuntimeError"
    assert state["scope"] == "api_process"
    assert model_tiers._CACHE == {}  # 失敗は固定しない


def test_success_does_not_count(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "src.storage.config_store.get_config", lambda *_a, **_k: {"fast": "custom:model"}
    )

    assert resolve_tier_model(Tier.FAST, db_path=tmp_path / "x.db") == "custom:model"
    assert tier_load_alarm.tier_load_failure_state()["count"] == 0


def test_failed_load_retries_after_window(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[int] = []

    def flaky(*_a: Any, **_k: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return {"fast": "custom:model"}

    monkeypatch.setattr("src.storage.config_store.get_config", flaky)
    db = tmp_path / "x.db"

    resolve_tier_model(Tier.FAST, db_path=db)
    resolve_tier_model(Tier.FAST, db_path=db)  # 窓の中は読み直さない
    assert len(calls) == 1
    monkeypatch.setattr(model_tiers, "_FAILURE_RETRY_SECONDS", 0.0)
    assert resolve_tier_model(Tier.FAST, db_path=db) == "custom:model"


def test_alarm_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> bool:
        raise RuntimeError("x")

    monkeypatch.setattr(tier_load_alarm, "_schedule_notice", boom)
    tier_load_alarm.record_tier_load_failure("E")  # 例外を出さない
    assert tier_load_alarm.tier_load_failure_state()["count"] == 1


def test_gate_not_consumed_when_scheduling_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tier_load_alarm, "_schedule_notice", lambda: False)
    tier_load_alarm.record_tier_load_failure("E")
    assert tier_load_alarm._last_notified == []


def test_thread_context_sends_notice_with_type_only(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    sent: list[str] = []
    done = threading.Event()

    async def fake_post(*, title: str, body: str, importance: str) -> bool:
        sent.append(body)
        done.set()
        return True

    monkeypatch.setattr("src.ui.services.ops_notify.post_ops_message", fake_post)

    tier_load_alarm.record_tier_load_failure("OperationalError")  # ループ無しの文脈

    assert done.wait(5)
    assert "OperationalError" not in sent[0]
    assert tier_load_alarm._last_notified == [date.today()]


async def test_async_failure_posts_ops_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    calls: list[str] = []

    async def fake_post(*, title: str, body: str, importance: str) -> bool:
        calls.append(title)
        return True

    monkeypatch.setattr("src.ui.services.ops_notify.post_ops_message", fake_post)

    tier_load_alarm.record_tier_load_failure("a")
    tier_load_alarm.record_tier_load_failure("b")
    await asyncio.gather(*list(tier_load_alarm._pending_notices))
    assert tier_load_alarm._last_notified == [date.today()]

    assert len(calls) == 1
    assert tier_load_alarm.tier_load_failure_state()["count"] == 2


def test_invalidate_clears_failure_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """保存の後の invalidate は失敗の待ちも捨て、すぐ DB を読み直す。"""
    model_tiers._FAILED_AT["None"] = time.monotonic()
    model_tiers.invalidate_model_tiers_cache()
    assert model_tiers._FAILED_AT == {}
