"""非サイバーの地政学・政策記事の重要度上限 (2026-10-24)。"""

from __future__ import annotations

import pytest

from src.cti.geopolitical_cap import cap_geopolitical_importance, cap_mode


@pytest.mark.parametrize("category", ["geopolitical", "policy"])
def test_high_noncyber_category_is_capped_to_medium_when_on(category: str) -> None:
    result = cap_geopolitical_importance("high", category, mode="on")

    assert result.importance == "medium"
    assert result.would_cap is True
    assert result.applied is True


def test_shadow_reports_would_cap_but_keeps_high() -> None:
    result = cap_geopolitical_importance("high", "geopolitical", mode="shadow")

    assert result.importance == "high"
    assert result.would_cap is True
    assert result.applied is False


def test_off_does_nothing() -> None:
    result = cap_geopolitical_importance("high", "geopolitical", mode="off")

    assert result.importance == "high"
    assert result.would_cap is False


@pytest.mark.parametrize("category", ["apt", "vulnerability", "incident", "malware", "breach"])
def test_cyber_categories_are_never_capped(category: str) -> None:
    result = cap_geopolitical_importance("high", category, mode="on")

    assert result.importance == "high"
    assert result.would_cap is False


@pytest.mark.parametrize("importance", ["medium", "low"])
def test_non_high_is_untouched(importance: str) -> None:
    result = cap_geopolitical_importance(importance, "geopolitical", mode="on")  # type: ignore[arg-type]

    assert result.importance == importance
    assert result.would_cap is False


def test_cyber_evidence_keeps_high_even_in_geopolitical_category() -> None:
    # IOC / ATT&CK 手法を伴う記事 = 国家系のサイバー作戦。下げない
    result = cap_geopolitical_importance("high", "geopolitical", mode="on", has_cyber_evidence=True)

    assert result.importance == "high"
    assert result.would_cap is False


def test_unknown_or_missing_category_is_not_capped() -> None:
    assert cap_geopolitical_importance("high", None, mode="on").importance == "high"
    assert cap_geopolitical_importance("high", "other", mode="on").importance == "high"


def test_category_match_is_case_insensitive() -> None:
    assert cap_geopolitical_importance("high", " Geopolitical ", mode="on").importance == "medium"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("on", "on"),
        ("1", "on"),
        ("shadow", "shadow"),
        ("off", "off"),
        ("0", "off"),
        ("garbage", "shadow"),
    ],
)
def test_cap_mode_parses_env(monkeypatch: pytest.MonkeyPatch, raw: str, expected: str) -> None:
    monkeypatch.setenv("GEOPOLITICAL_HIGH_CAP", raw)

    assert cap_mode() == expected


def test_cap_mode_defaults_to_shadow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEOPOLITICAL_HIGH_CAP", raising=False)

    assert cap_mode() == "shadow"
