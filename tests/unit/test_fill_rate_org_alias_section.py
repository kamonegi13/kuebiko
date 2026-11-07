"""別名辞書の鮮度行 (fill-rate 監査)。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.ui.services.eventnews_org_alias import ALIAS_SOURCE_ENV
from src.ui.services.fill_rate_org_alias_section import OrgAliasStats, org_alias_line

_NOW = datetime(2026, 11, 1, tzinfo=UTC)


def _stats(rows: int, age_days: float | None) -> OrgAliasStats:
    last = (_NOW - timedelta(days=age_days)).isoformat() if age_days is not None else None
    return OrgAliasStats(rows, last, 1, 0, 2)


def test_pairs_source_is_informational_even_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ALIAS_SOURCE_ENV, raising=False)
    line, warn = org_alias_line(_stats(0, None), now=_NOW)
    assert not warn and "参考" in line


def test_dict_source_warns_when_empty_or_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALIAS_SOURCE_ENV, "dict")
    assert org_alias_line(_stats(0, None), now=_NOW)[1]
    assert org_alias_line(_stats(5, 20), now=_NOW)[1]


def test_dict_source_fresh_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALIAS_SOURCE_ENV, "dict")
    line, warn = org_alias_line(_stats(5, 3), now=_NOW)
    assert not warn and "+1/~0/-2" in line
