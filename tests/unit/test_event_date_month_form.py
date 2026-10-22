"""EVENT_DATE_MONTH_FORM (s24b/s24c の切替、2026-10-22)。

vnext の判断プロンプトは月のみを ``YYYY-MM`` で出すので ``YYYY-MM-01`` は本当の 1 日になる。
旧モデル (s23i) は月のみを ``-01`` で出すため、**flag が無いときは従来どおり unknown**。
"""

from __future__ import annotations

from datetime import date

import pytest

from src.pipeline.summary import _normalize_temporal, event_date_precision

_REF = date(2026, 6, 27)
_FLAG = "EVENT_DATE_MONTH_FORM"


def test_first_of_month_is_unknown_without_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_FLAG, raising=False)
    assert event_date_precision("2026-03-01", "2026-03-01", "occurred") == "unknown"


def test_first_of_month_is_day_with_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_FLAG, "1")
    assert event_date_precision("2026-03-01", "2026-03-01", "occurred") == "day"
    # 月のみ・他の日は flag に依らない
    assert event_date_precision("2026-03", "2026-03-01", "occurred") == "month"
    assert event_date_precision("2026-03-10", "2026-03-10", "occurred") == "day"


def test_relative_without_date_dropped_without_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_FLAG, raising=False)
    ev, basis, _ = _normalize_temporal(None, "relative", None, reference=_REF)
    assert (ev, basis) == (None, None)
    assert event_date_precision(None, None, "relative") is None


def test_relative_without_date_kept_with_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_FLAG, "1")
    ev, basis, _ = _normalize_temporal(None, "relative", None, reference=_REF)
    assert (ev, basis) == (None, "relative")
    assert event_date_precision(None, None, "relative") == "relative"
    # relative 以外の basis は日付があるときだけ (従来どおり)
    assert _normalize_temporal(None, "occurred", None, reference=_REF)[1] is None
