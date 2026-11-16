"""インドの数の単位の照合 (src/cti/indian_units.py + body_translator の影の記録の配線)。"""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from src.cti import indian_units
from src.cti.body_translator import translate_body
from src.cti.indian_units import (
    check_indian_units,
    extract_indian_amounts,
    indian_units_mode,
    parse_japanese_numbers,
    shadow_check_indian_units,
)
from tests.unit.test_body_translator import FakeLLM


def test_mode_defaults_and_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRANSLATE_INDIAN_UNITS_CHECK", raising=False)
    assert indian_units_mode() == "0"
    monkeypatch.setenv("TRANSLATE_INDIAN_UNITS_CHECK", "shadow")
    assert indian_units_mode() == "shadow"
    monkeypatch.setenv("TRANSLATE_INDIAN_UNITS_CHECK", "1")
    assert indian_units_mode() == "0"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("A fine of ₹500 crore was imposed.", [5e9]),
        ("Rs 10 Crores lost", [1e8]),
        ("about 2.5 lakh users", [250_000.0]),
        ("1 lakh crore fund", [1e12]),
        ("1,200 crore and 3 lakhs", [1.2e10, 300_000.0]),
        ("1 lakh-crore scheme", [1e12]),
        ("no amounts here, 500 users", []),
        ("flakhead 5 crores", [5e7]),
    ],
)
def test_extract_indian_amounts(source: str, expected: list[float]) -> None:
    assert extract_indian_amounts(source) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("50億ルピー", [5e9]),
        ("1,000万人", [1e7]),
        ("2.5万件", [25_000.0]),
        ("1兆円", [1e12]),
        ("250万と50億", [2.5e6, 5e9]),
        ("1億2,300万円", [123_000_000.0]),
        ("21万7,295件", [217_295.0]),
        ("1兆2,000億", [1.2e12]),
        ("3千万", [3e7]),
        ("12,345 件", [12_345.0]),
    ],
)
def test_parse_japanese_numbers(text: str, expected: list[float]) -> None:
    assert parse_japanese_numbers(text) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("source", "translated", "mismatched"),
    [
        ("₹500 crore", "50億ルピー", 0),
        ("₹500 crore", "500万ルピー", 1),
        ("1 lakh crore", "1兆ルピー", 0),
        ("1 lakh crore", "1,000億ルピー", 1),
        ("2.5 lakh", "25万人", 0),
        ("2.5 lakh", "250万人", 1),
        ("Rs 10 crore and 2 lakh", "1億ルピーと20万", 0),
        ("Rs 10 crore and 2 lakh", "1億ルピーと200万", 1),
        ("5 crore", "約5億0000万円", 1),
        ("5 crore", "5 crore ルピー", 1),
    ],
)
def test_check_match_and_mismatch(source: str, translated: str, mismatched: int) -> None:
    result = check_indian_units(source, translated)
    assert result.applicable
    assert result.mismatched == mismatched


def test_tolerance_is_one_percent() -> None:
    assert check_indian_units("1 lakh", "10.05万").mismatched == 0
    assert check_indian_units("1 lakh", "10.5万").mismatched == 1


def test_not_applicable_without_indian_units() -> None:
    result = check_indian_units("Losses reached 500 million dollars.", "損失は5億ドル")
    assert not result.applicable
    assert result.mismatched == 0


def test_shadow_logs_numbers_only() -> None:
    with capture_logs() as logs:
        shadow_check_indian_units(
            "A secret body: ₹500 crore", "秘密の本文 500万ルピー", article_id="a1", model="m"
        )
    events = [e for e in logs if e["event"] == "translate_indian_units_check"]
    assert len(events) == 1
    ev = events[0]
    assert ev["amounts"] == 1 and ev["mismatched"] == 1
    assert ev["expected"] == [5e9] and ev["ja_numbers"] == [5e6]
    assert "secret" not in repr(ev) and "秘密" not in repr(ev)


def test_shadow_not_applicable_is_silent() -> None:
    with capture_logs() as logs:
        shadow_check_indian_units("plain text", "平文")
    assert logs == []


def test_shadow_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(source: str, translated: str) -> None:
        raise RuntimeError("x")

    monkeypatch.setattr(indian_units, "check_indian_units", boom)
    with capture_logs() as logs:
        shadow_check_indian_units("5 crore", "5億")
    assert [e["event"] for e in logs] == ["translate_indian_units_check_failed"]
    assert logs[0]["error_type"] == "RuntimeError"


SOURCE = "The firm was fined ₹500 crore by the regulator after the breach was disclosed. " * 2


@pytest.mark.asyncio
async def test_flag_off_makes_no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRANSLATE_INDIAN_UNITS_CHECK", raising=False)
    called: list[int] = []
    monkeypatch.setattr(
        "src.cti.body_translator.shadow_check_indian_units", lambda *a, **k: called.append(1)
    )
    await translate_body(FakeLLM(), SOURCE)
    assert called == []


@pytest.mark.asyncio
async def test_shadow_never_alters_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSLATE_INDIAN_UNITS_CHECK", "0")
    baseline = await translate_body(FakeLLM(), SOURCE)
    monkeypatch.setenv("TRANSLATE_INDIAN_UNITS_CHECK", "shadow")
    with capture_logs() as logs:
        shadowed = await translate_body(FakeLLM(), SOURCE)
    assert shadowed == baseline
    assert any(e["event"] == "translate_indian_units_check" for e in logs)
