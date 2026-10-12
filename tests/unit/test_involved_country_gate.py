"""関与国の付けすぎ関門 (src/cti/involved_country_gate.py)。"""

from __future__ import annotations

import pytest

from src.cti.involved_country_gate import (
    GATE_ENV,
    MODE_OFF,
    MODE_ON,
    MODE_SHADOW,
    filter_involved_countries,
    gate_mode,
)
from src.cti.nation_gazetteer import country_mention_counts


def test_cyber_category_returns_empty() -> None:
    isos = ["US", "CN"]
    assert filter_involved_countries(isos, "malware", "China and US hackers") == []
    assert isos == ["US", "CN"]  # 入力は変えない


def test_geopolitical_keeps_leading_three_regardless_of_mentions() -> None:
    assert filter_involved_countries(["US", "CN", "RU"], "geopolitical", "no names here") == [
        "US",
        "CN",
        "RU",
    ]


def test_fourth_and_later_dropped_when_barely_mentioned() -> None:
    text = "Russia Russia Russia Ukraine Ukraine Germany once"
    kept = filter_involved_countries(
        ["RU", "UA", "US", "DE"], "geopolitical", text + " France France France"
    )
    assert kept == ["RU", "UA", "US"]


def test_fourth_kept_when_mentioned_enough() -> None:
    text = "Germany Germany Germany announced"
    kept = filter_involved_countries(["RU", "UA", "US", "DE"], "policy", text)
    assert kept == ["RU", "UA", "US", "DE"]


def test_japan_is_exempt_from_mention_filter() -> None:
    kept = filter_involved_countries(["RU", "UA", "US", "JP"], "geopolitical", "x")
    assert kept == ["RU", "UA", "US", "JP"]


def test_japan_dropped_for_cyber_category() -> None:
    assert filter_involved_countries(["JP"], "breach", "日本 日本 日本") == []


def test_mention_counts_dedupe_overlapping_aliases() -> None:
    counts = country_mention_counts("Russia attacked. Russia again.")
    assert counts["RU"] == 2
    assert country_mention_counts("") == {}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, MODE_SHADOW),
        ("1", MODE_ON),
        ("on", MODE_ON),
        ("bogus", MODE_SHADOW),
        ("0", MODE_OFF),
        ("shadow", MODE_SHADOW),
        ("SHADOW", MODE_SHADOW),
    ],
)
def test_gate_mode(monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: str) -> None:
    if raw is None:
        monkeypatch.delenv(GATE_ENV, raising=False)
    else:
        monkeypatch.setenv(GATE_ENV, raw)
    assert gate_mode() == expected
