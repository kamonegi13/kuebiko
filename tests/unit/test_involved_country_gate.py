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


# --- 規則 3 の例外 (2026-10-24): 参加・署名・支援の動詞と同じ文にある国は言及が薄くても残す ---

from src.cti.involved_country_gate import (  # noqa: E402
    CUE_EXCEPTION_ENV,
    cue_exception_enabled,
)

_SIGNERS = ["US", "GB", "AU", "CA", "FR"]


def test_cue_exception_keeps_signatory_in_same_sentence() -> None:
    text = "The joint statement was signed by the US, UK, Australia, Canada and France."
    kept = filter_involved_countries(_SIGNERS, "policy", text, cue_exception=True)
    assert kept == _SIGNERS


def test_cue_exception_off_drops_signatory() -> None:
    text = "The joint statement was signed by the US, UK, Australia, Canada and France."
    kept = filter_involved_countries(_SIGNERS, "policy", text, cue_exception=False)
    assert kept == ["US", "GB", "AU"]


def test_cue_in_other_sentence_does_not_rescue() -> None:
    text = "Canada and France appear here. Separately, the group signed a deal."
    kept = filter_involved_countries(_SIGNERS, "policy", text, cue_exception=True)
    assert "CA" not in kept and "FR" not in kept


def test_japanese_cue_keeps_participant() -> None:
    text = "合同演習にはカナダとフランスが参加した。"
    kept = filter_involved_countries(_SIGNERS, "geopolitical", text, cue_exception=True)
    assert kept == _SIGNERS[:3] + ["CA", "FR"]


def test_cue_exception_never_applies_to_cyber_category() -> None:
    text = "France signed the statement."
    assert filter_involved_countries(["FR"], "malware", text, cue_exception=True) == []


def test_cue_exception_env_default_on_and_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CUE_EXCEPTION_ENV, raising=False)
    assert cue_exception_enabled() is True
    monkeypatch.setenv(CUE_EXCEPTION_ENV, "0")
    assert cue_exception_enabled() is False


def test_mention_positions_consistent_with_counts() -> None:
    from src.cti.nation_gazetteer import country_mention_positions

    text = "Russia attacked. Russia again."
    _, pos = country_mention_positions(text)
    assert len(pos["RU"]) == country_mention_counts(text)["RU"] == 2
