"""日本の同盟・同志国の区分 (config/cti/countries.yaml の ``alliance`` が SSoT)。

根拠の「直接 (日本と同盟国が被害) / 間接 (それ以外)」の印の土台。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.cti import alliance
from src.cti.alliance import (
    PROXIMITY_DIRECT,
    PROXIMITY_INDIRECT,
    PROXIMITY_UNKNOWN,
    allied_nations,
    evidence_proximity,
    load_allied_from_yaml,
)
from src.cti.threat_actor_doctrine import ALLIED_NATIONS, STATE_NATIONS

# 2026-10-22 時点の区分 (旧 threat_actor_doctrine.ALLIED_NATIONS と同一 = 挙動不変)
_EXPECTED = {"us", "gb", "au", "ca", "nz", "kr", "tw", "ph"}


class TestAlliedNations:
    def test_matches_previous_literal_set(self) -> None:
        assert allied_nations() == frozenset(_EXPECTED)

    def test_doctrine_constant_is_derived_from_yaml(self) -> None:
        assert allied_nations() == ALLIED_NATIONS

    def test_disjoint_from_adversaries_and_home(self) -> None:
        assert not (allied_nations() & STATE_NATIONS)
        assert "jp" not in allied_nations()

    def test_every_allied_code_is_a_known_country(self) -> None:
        from src.cti.taxonomy_normalizer import load_country_display

        display = load_country_display()
        assert {c.upper() for c in allied_nations()} <= set(display)


class TestLoader:
    def test_reads_only_ally_marked_countries(self, tmp_path: Path) -> None:
        yml = tmp_path / "countries.yaml"
        yml.write_text(
            "canonical:\n"
            "  JP: {display: 日本}\n"
            "  US: {display: 米国, alliance: ally}\n"
            "  FR: {display: フランス}\n"
            "  XX: {display: 不明, alliance: bogus}\n",
            encoding="utf-8",
        )
        assert load_allied_from_yaml(yml) == frozenset({"us"})

    def test_missing_file_gives_empty_set_not_exception(self, tmp_path: Path) -> None:
        assert load_allied_from_yaml(tmp_path / "nope.yaml") == frozenset()


class TestEvidenceProximity:
    @pytest.mark.parametrize("iso", ["JP", "jp", "US", "kr", "TW"])
    def test_japan_and_allies_are_direct(self, iso: str) -> None:
        assert evidence_proximity(iso) == PROXIMITY_DIRECT

    @pytest.mark.parametrize("iso", ["PL", "UA", "DE", "BR"])
    def test_other_countries_are_indirect(self, iso: str) -> None:
        assert evidence_proximity(iso) == PROXIMITY_INDIRECT

    @pytest.mark.parametrize("iso", [None, "", "  "])
    def test_missing_victim_country_is_unknown(self, iso: str | None) -> None:
        assert evidence_proximity(iso) == PROXIMITY_UNKNOWN

    def test_enabled_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("STANDING_EVIDENCE_PROXIMITY", raising=False)
        assert alliance.proximity_enabled() is True
        monkeypatch.setenv("STANDING_EVIDENCE_PROXIMITY", "0")
        assert alliance.proximity_enabled() is False
