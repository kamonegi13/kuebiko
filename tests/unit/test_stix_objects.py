"""STIX 2.1 の SDO / SRO の組み立て (2026-09-27、旧 test_stix_exporter の観点を引き継ぐ)。"""

from __future__ import annotations

import uuid

import pytest

from src.cti.actor_normalizer import ActorAlias
from src.cti.attack_techniques import TechniqueInfo
from src.cti.stix import objects as o
from src.cti.stix.core import REFERENCE_TS, confidence_value, stix_id, to_ts


def test_ids_are_deterministic_uuid_v4_shape() -> None:
    a, b = stix_id("malware", "x-agent"), stix_id("malware", "x-agent")

    assert a == b
    assert uuid.UUID(a.split("--", 1)[1]).version == 4


def test_indicator_patterns() -> None:
    ip = o.indicator_object("ipv4", "203.0.113.5", valid_from=REFERENCE_TS)
    url = o.indicator_object("url", "https://evil.example/a'b", valid_from=REFERENCE_TS)
    sha = o.indicator_object("sha256", "a" * 64, valid_from=REFERENCE_TS)

    assert ip is not None and ip["pattern"] == "[ipv4-addr:value = '203.0.113.5']"
    assert url is not None and url["pattern"] == "[url:value = 'https://evil.example/a\\'b']"
    assert sha is not None and sha["pattern"] == f"[file:hashes.'SHA-256' = '{'a' * 64}']"
    assert o.indicator_object("email", "x@example.com", valid_from=REFERENCE_TS) is None


def test_vulnerability_has_cve_reference() -> None:
    v = o.vulnerability_object("cve-2026-1234")

    assert v["name"] == "CVE-2026-1234"
    assert v["external_references"][0] == {
        "source_name": "cve",
        "external_id": "CVE-2026-1234",
        "url": "https://nvd.nist.gov/vuln/detail/CVE-2026-1234",
    }


def test_attack_pattern_uses_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.cti.attack_techniques.load_technique_catalog",
        lambda *a, **k: {
            "T1566.001": TechniqueInfo(
                "T1566.001", "Spearphishing Attachment", ("initial-access",), "T1566"
            )
        },
    )

    named = o.attack_pattern_object("t1566.001")
    bare = o.attack_pattern_object("T1003")

    assert named["name"] == "T1566.001 Spearphishing Attachment"
    assert named["kill_chain_phases"] == [
        {"kill_chain_name": "mitre-attack", "phase_name": "initial-access"}
    ]
    assert "T1566/001" in named["external_references"][0]["url"]
    assert bare["name"] == "T1003"
    assert "kill_chain_phases" not in bare


def test_group_is_intrusion_set_with_mitre_reference() -> None:
    obj = o.actor_object(
        ActorAlias(id="apt28", canonical="APT28", mitre_group="G0007", nation="ru"),
        primary_motivation="organizational-gain",
    )

    assert obj["type"] == "intrusion-set"
    assert obj["primary_motivation"] == "organizational-gain"
    assert obj["external_references"][0]["external_id"] == "G0007"


def test_organization_and_contractor_are_threat_actors() -> None:
    for kind in ("organization", "contractor"):
        obj = o.actor_object(ActorAlias(id=f"x_{kind}", canonical="X", kind=kind, nation="ru"))
        assert obj["type"] == "threat-actor"
        assert obj["threat_actor_types"] == ["nation-state"]


def test_sector_vocabulary_comes_from_sector_ssot() -> None:
    assert o.sector_stix("financial") == ("金融", "financial-services")
    assert o.sector_stix("multi_sector") is None  # STIX の業種語彙に対応が無い


def test_confidence_scale_and_timestamps() -> None:
    assert (confidence_value("high"), confidence_value("medium"), confidence_value("low")) == (
        85,
        50,
        15,
    )
    assert confidence_value(None) is None
    assert to_ts("2026-09-27T01:02:03.456789+00:00") == "2026-09-27T01:02:03.456Z"
    assert to_ts("not a date") is None
