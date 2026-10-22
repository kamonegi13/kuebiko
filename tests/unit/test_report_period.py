"""月次レポートの期間の読み取り (まとめ記事の判定 roundup_detector と同じ語彙)。"""

from __future__ import annotations

import pytest

from src.cti.roundup_detector import report_period


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("2026年8月 韓国 APT 攻撃脅威動向レポート", "2026-08"),
        ("2026年7月 APT グループ脅威トレンドレポート", "2026-07"),
        ("Monthly Threat Report: August 2026", "2026-08"),
        ("Ransomware trends report 2026-09", "2026-09"),
    ],
)
def test_reads_the_period_of_a_periodic_report(title: str, expected: str) -> None:
    assert report_period(title) == expected


@pytest.mark.parametrize(
    "title",
    [
        "12 October 2026 Critical Vulnerability in Citrix NetScaler",  # 月はあるがレポートでない
        "2026年8月に発生した障害について",
        "Threat report with no date",
        "",
    ],
)
def test_returns_none_for_non_reports_or_undated_titles(title: str) -> None:
    assert report_period(title) is None
