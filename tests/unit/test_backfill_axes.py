"""scripts/backfill_axes.py の純粋ロジック (正規化 / cron guard) のテスト。"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from backfill_axes import guard_reason, normalize_axes  # noqa: E402

from src.cti.analysis_axes_classifier import AnalysisAxesOut


class TestNormalizeAxes:
    def test_unknown_intent_returns_none_fields(self) -> None:
        out = AnalysisAxesOut(intent="unknown", confidence="high", rationale="x")

        intent, confidence, rationale, _, _, _, _ = normalize_axes(out, published=date(2026, 7, 1))

        assert intent is None
        assert confidence is None
        assert rationale is None

    def test_valid_intent_and_technical(self) -> None:
        out = AnalysisAxesOut(
            intent="prepositioning",
            confidence="low",
            rationale="弱シグナル",
            technical="VPN 経由で潜伏",
        )

        intent, confidence, rationale, technical, _, _, _ = normalize_axes(
            out, published=date(2026, 7, 1)
        )

        assert intent == "prepositioning"
        assert confidence == "low"
        assert rationale == "弱シグナル"
        assert technical == "VPN 経由で潜伏"

    def test_event_date_after_published_is_dropped(self) -> None:
        out = AnalysisAxesOut(event_date="2026-07-15", event_date_basis="occurred")

        _, _, _, _, ev, basis, _ = normalize_axes(out, published=date(2026, 7, 1))

        assert ev is None
        assert basis is None

    def test_reported_basis_anchored_to_published_passes(self) -> None:
        out = AnalysisAxesOut(event_date="2026-07-01", event_date_basis="reported")

        _, _, _, _, ev, basis, _ = normalize_axes(out, published=date(2026, 7, 1))

        assert ev == "2026-07-01"
        assert basis == "reported"

    def test_compromise_after_event_is_dropped(self) -> None:
        out = AnalysisAxesOut(
            event_date="2026-06-20",
            event_date_basis="disclosed",
            compromise_date="2026-06-25",
        )

        _, _, _, _, ev, _, comp = normalize_axes(out, published=date(2026, 7, 1))

        assert ev == "2026-06-20"
        assert comp is None


class TestGuardReason:
    def test_hourly_rss_window(self) -> None:
        assert guard_reason(datetime(2026, 7, 13, 12, 58)) == "hourly-rss"
        assert guard_reason(datetime(2026, 7, 13, 12, 10)) == "hourly-rss"

    def test_quiet_window_morning(self) -> None:
        assert guard_reason(datetime(2026, 7, 13, 6, 30)) == "morning-brief"

    def test_clear_window(self) -> None:
        assert guard_reason(datetime(2026, 7, 13, 12, 30)) is None
