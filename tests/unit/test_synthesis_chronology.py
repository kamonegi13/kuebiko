"""src.synthesis.grounded.chronology のテスト (発生日時の前後関係を事実として供給)。"""

from __future__ import annotations

from datetime import datetime

from src.synthesis.grounded.chronology import article_chronology


class TestChronology:
    def test_fresh_event_not_resurfaced(self) -> None:
        # 事象日が報道日に近い → 再浮上でない (新規事象)
        c = article_chronology(
            report="2026-07-01 12:00:00+00", event_date="2026-06-30", event_date_basis="disclosed"
        )
        assert c.resurfaced is False
        assert c.report_short == "07-01"
        assert "2026-06-30" in c.label
        assert "再浮上" not in c.label

    def test_old_event_is_resurfaced(self) -> None:
        # 事象日が報道日より大きく前 → 再浮上 (旧事案の再報道)
        c = article_chronology(
            report="2026-07-01 12:00:00+00", event_date="2024-03-15", event_date_basis="occurred"
        )
        assert c.resurfaced is True
        assert "再浮上" in c.label
        assert "occurred" in c.label

    def test_no_event_date(self) -> None:
        c = article_chronology(report="2026-07-01 12:00:00+00", event_date=None)
        assert c.resurfaced is False
        assert c.event_short == ""
        assert "事象日 不明" in c.label

    def test_partial_event_date_year_month(self) -> None:
        # "2024-03" (日欠落) でもパースし、再浮上判定できる
        c = article_chronology(report="2026-07-01 12:00:00+00", event_date="2024-03")
        assert c.resurfaced is True

    def test_datetime_report_accepted(self) -> None:
        c = article_chronology(
            report=datetime.fromisoformat("2026-07-01 09:00:00+00"), event_date="2026-07-01"
        )
        assert c.report_short == "07-01"
        assert c.resurfaced is False

    def test_garbage_dates_degrade_gracefully(self) -> None:
        c = article_chronology(report="not-a-date", event_date="garbage")
        assert c.report_short == "??"
        assert c.resurfaced is False
        assert c.event_short == ""

    def test_boundary_exactly_14_days_not_resurfaced(self) -> None:
        # 14 日ちょうどは閾値超えでない (> のみ)
        c = article_chronology(report="2026-07-15 00:00:00+00", event_date="2026-07-01")
        assert c.resurfaced is False
