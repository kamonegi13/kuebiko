"""ATT&CK の技術 → 名前・戦術・親技術の辞書 (2026-09-27)。

記事の TTP は ID だけで、名前も戦術 (kill chain) も親技術との関係も無かった。
MITRE の STIX データには戦術があるのに、同期はアクターの TTP の表示名にしか使っていなかった。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.cti.attack_techniques import (
    TechniqueInfo,
    load_technique_catalog,
    parse_technique_catalog,
    save_technique_catalog,
)

_OBJECTS: list[dict[str, Any]] = [
    {
        "type": "attack-pattern",
        "id": "attack-pattern--1",
        "name": "Phishing",
        "external_references": [{"source_name": "mitre-attack", "external_id": "T1566"}],
        "kill_chain_phases": [{"kill_chain_name": "mitre-attack", "phase_name": "initial-access"}],
    },
    {
        "type": "attack-pattern",
        "id": "attack-pattern--2",
        "name": "Spearphishing Attachment",
        "x_mitre_is_subtechnique": True,
        "external_references": [{"source_name": "mitre-attack", "external_id": "T1566.001"}],
        "kill_chain_phases": [{"kill_chain_name": "mitre-attack", "phase_name": "initial-access"}],
    },
    {
        "type": "attack-pattern",
        "id": "attack-pattern--3",
        "name": "Old",
        "revoked": True,
        "external_references": [{"source_name": "mitre-attack", "external_id": "T9999"}],
    },
    {
        "type": "attack-pattern",
        "id": "attack-pattern--4",
        "name": "Old Phishing",
        "revoked": True,
        "external_references": [{"source_name": "mitre-attack", "external_id": "T1192"}],
    },
    {
        "type": "relationship",
        "relationship_type": "revoked-by",
        "source_ref": "attack-pattern--4",
        "target_ref": "attack-pattern--2",
    },
]


def test_parse_catalog() -> None:
    cat = parse_technique_catalog(_OBJECTS)
    assert cat["T1566"] == TechniqueInfo("T1566", "Phishing", ("initial-access",), None)
    assert cat["T1566.001"].parent == "T1566"
    assert "T9999" not in cat  # 置き換え先の無い取り消しは載せない
    old = cat["T1192"]  # 取り消された ID は置き換え先の名前・戦術で読める
    assert (old.name, old.replaced_by, old.tactics) == (
        "Spearphishing Attachment",
        "T1566.001",
        ("initial-access",),
    )


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "attack_techniques.json"
    save_technique_catalog(parse_technique_catalog(_OBJECTS), p)
    load_technique_catalog.cache_clear()
    cat = load_technique_catalog(str(p))
    assert cat["T1566.001"].tactics == ("initial-access",)


def test_missing_catalog_is_empty(tmp_path: Path) -> None:
    load_technique_catalog.cache_clear()
    assert load_technique_catalog(str(tmp_path / "none.json")) == {}
