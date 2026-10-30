"""model_tiers の DB 読み込み失敗の警報 (TIER-LOAD-ALARM)。"""

from __future__ import annotations

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
    tier_load_alarm.reset_for_tests()
    yield
    model_tiers._CACHE.clear()
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
    assert "readonly" in str(state["last_error"])


def test_success_does_not_count(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "src.storage.config_store.get_config", lambda *_a, **_k: {"fast": "custom:model"}
    )

    assert resolve_tier_model(Tier.FAST, db_path=tmp_path / "x.db") == "custom:model"
    assert tier_load_alarm.tier_load_failure_state()["count"] == 0


def test_notify_gate_once_per_day() -> None:
    day = date(2026, 10, 30)
    assert tier_load_alarm._should_notify_today(day) is True
    assert tier_load_alarm._should_notify_today(day) is False
    assert tier_load_alarm._should_notify_today(date(2026, 10, 31)) is True


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

    assert len(calls) == 1
    assert tier_load_alarm.tier_load_failure_state()["count"] == 2
