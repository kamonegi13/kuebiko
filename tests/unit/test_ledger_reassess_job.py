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
