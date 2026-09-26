"""毎時の台帳増分再評価 — flag と cap の解釈 (2026-09-17)。"""

from __future__ import annotations

import pytest

from src.ui.services.ledger_reassess_job import hourly_reassess_cap, hourly_reassess_enabled


def test_flag_default_on_and_cap_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEDGER_REASSESS_HOURLY", raising=False)
    monkeypatch.delenv("LEDGER_REASSESS_HOURLY_CAP", raising=False)
    assert hourly_reassess_enabled() is True
    assert hourly_reassess_cap() == 6


def test_flag_off_and_cap_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEDGER_REASSESS_HOURLY", "0")
    monkeypatch.setenv("LEDGER_REASSESS_HOURLY_CAP", "10")
    assert hourly_reassess_enabled() is False
    assert hourly_reassess_cap() == 10
    monkeypatch.setenv("LEDGER_REASSESS_HOURLY_CAP", "abc")
    assert hourly_reassess_cap() == 6


async def test_skips_without_evaluating_when_ledger_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    """定時 run が台帳を更新中なら評価せずに飛ばす (2026-09-26、並行評価の防止)。"""
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    from src import config_loader
    from src.assessment import ledger, ledger_lock, stateful
    from src.ui.services import ledger_reassess_job

    @asynccontextmanager
    async def busy(**_: object) -> AsyncIterator[bool]:
        yield False

    async def must_not_run(**_: object) -> None:
        raise AssertionError("lock が取れないのに評価した")

    monkeypatch.delenv("LEDGER_REASSESS_HOURLY", raising=False)
    monkeypatch.setattr(ledger, "ledger_mode", lambda: "on")
    monkeypatch.setattr(ledger_lock, "ledger_write_lock", busy)
    monkeypatch.setattr(stateful, "build_estimate_stateful", must_not_run)
    monkeypatch.setattr(config_loader, "load_app_config", lambda: None)

    result = await ledger_reassess_job.run_ledger_reassess_hourly()

    assert result == {"skipped": "ledger_busy"}
