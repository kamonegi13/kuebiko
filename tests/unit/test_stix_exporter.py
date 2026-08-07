"""src.cti.stix_exporter のテスト (Phase 4)。"""

from __future__ import annotations

import json

from src.cti.actor_normalizer import ActorAlias
from src.cti.ioc_extractor import ExtractedIocs
from src.cti.stix_exporter import (
    attack_pattern_for_technique,
    indicator_for_domain,
    indicator_for_hash,
    indicator_for_ipv4,
    indicator_for_url,
    threat_actor_for,
    to_bundle,
    vulnerability_for_cve,
)

# ---------- 単体オブジェクト ----------


def test_indicator_ipv4_format() -> None:
    obj = indicator_for_ipv4("8.8.8.8")
    assert obj["type"] == "indicator"
    assert obj["spec_version"] == "2.1"
    assert obj["pattern"] == "[ipv4-addr:value = '8.8.8.8']"
    assert obj["pattern_type"] == "stix"
    assert obj["id"].startswith("indicator--")


def test_indicator_domain_format() -> None:
    obj = indicator_for_domain("evil.example.com")
    assert obj["pattern"] == "[domain-name:value = 'evil.example.com']"


def test_indicator_url_escapes_quotes() -> None:
    obj = indicator_for_url("https://evil.com/path'with'quote")
    assert "\\'" in obj["pattern"]


def test_indicator_hash_md5() -> None:
    obj = indicator_for_hash("5d41402abc4b2a76b9719d911017c592", "MD5")
    assert obj["pattern"].startswith("[file:hashes.'MD5'")
    assert "5d41402abc4b2a76b9719d911017c592" in obj["pattern"]


def test_vulnerability_for_cve() -> None:
    obj = vulnerability_for_cve("CVE-2024-12345")
    assert obj["type"] == "vulnerability"
    assert obj["name"] == "CVE-2024-12345"
    assert obj["external_references"][0]["source_name"] == "cve"
    assert "nvd.nist.gov" in obj["external_references"][0]["url"]


def test_threat_actor_with_mitre_group() -> None:
    actor = ActorAlias(
        id="apt29",
        canonical="APT29",
        aliases=("Cozy Bear", "Midnight Blizzard"),
        mitre_group="G0016",
        nation="ru",
        description="ロシア対外情報系 APT",
    )
    obj = threat_actor_for(actor)
    assert obj["type"] == "threat-actor"
    assert obj["name"] == "APT29"
    assert "Cozy Bear" in obj["aliases"]
    assert obj["external_references"][0]["external_id"] == "G0016"
    # motivation 未指定なら primary_motivation キーは付かない
    assert "primary_motivation" not in obj


def test_threat_actor_with_primary_motivation() -> None:
    # Phase Diamond-Axes: socio-political 軸 → STIX primary_motivation
    actor = ActorAlias(id="apt29", canonical="APT29")
    obj = threat_actor_for(actor, primary_motivation="organizational-gain")
    assert obj["primary_motivation"] == "organizational-gain"


def test_bundle_maps_socio_political_intent_to_motivation() -> None:
    # Phase Diamond-Axes: intent=espionage → threat-actor.primary_motivation
    iocs = ExtractedIocs(cves=("CVE-2024-1234",))
    actors = [ActorAlias(id="apt41", canonical="APT41")]
    bundle = to_bundle(iocs, actors, socio_political_intent="espionage")
    ta = next(o for o in bundle["objects"] if o["type"] == "threat-actor")
    assert ta["primary_motivation"] == "organizational-gain"


def test_bundle_unknown_intent_omits_motivation() -> None:
    iocs = ExtractedIocs(cves=("CVE-2024-1234",))
    actors = [ActorAlias(id="apt41", canonical="APT41")]
    bundle = to_bundle(iocs, actors, socio_political_intent="unknown")
    ta = next(o for o in bundle["objects"] if o["type"] == "threat-actor")
    assert "primary_motivation" not in ta


def test_attack_pattern_for_technique() -> None:
    obj = attack_pattern_for_technique("T1059.003")
    assert obj["type"] == "attack-pattern"
    assert obj["name"] == "MITRE ATT&CK T1059.003"
    # 階層 ID は URL でスラッシュに変換される (例: /techniques/T1059/003/)
    assert "T1059/003" in obj["external_references"][0]["url"]


# ---------- Bundle ----------


def test_bundle_structure() -> None:
    iocs = ExtractedIocs(
        cves=("CVE-2024-1234",),
        ipv4=("198.51.100.1",),
        domains=("evil.example.com",),
        md5=("5d41402abc4b2a76b9719d911017c592",),
        mitre_techniques=("T1078",),
    )
    actors = [
        ActorAlias(id="apt29", canonical="APT29", mitre_group="G0016"),
    ]
    bundle = to_bundle(iocs, actors, description="test bundle")
    assert bundle["type"] == "bundle"
    assert bundle["id"].startswith("bundle--")
    types = {o["type"] for o in bundle["objects"]}
    # 各種オブジェクトが含まれていること
    assert "identity" in types  # producer
    assert "vulnerability" in types
    assert "indicator" in types
    assert "threat-actor" in types
    assert "attack-pattern" in types
    assert "note" in types


def test_bundle_serializable_to_json() -> None:
    """生成された bundle が JSON シリアライズ可能であること (TIP 連携前提)。"""
    iocs = ExtractedIocs(cves=("CVE-2024-1234",), ipv4=("8.8.8.8",))
    bundle = to_bundle(iocs, [])
    text = json.dumps(bundle)
    parsed = json.loads(text)
    assert parsed["type"] == "bundle"


def test_bundle_deterministic_ids() -> None:
    """同じ入力なら同じ ID で出力される (uuid5 由来)。"""
    iocs1 = ExtractedIocs(cves=("CVE-2024-1234",))
    iocs2 = ExtractedIocs(cves=("CVE-2024-1234",))
    b1 = to_bundle(iocs1, [], description="x")
    b2 = to_bundle(iocs2, [], description="x")
    # vulnerability の ID が同じ
    cve_ids_1 = [o["id"] for o in b1["objects"] if o["type"] == "vulnerability"]
    cve_ids_2 = [o["id"] for o in b2["objects"] if o["type"] == "vulnerability"]
    assert cve_ids_1 == cve_ids_2


def test_bundle_empty() -> None:
    iocs = ExtractedIocs()
    bundle = to_bundle(iocs, [])
    # producer identity だけは入る
    types = {o["type"] for o in bundle["objects"]}
    assert types == {"identity"}


def test_bundle_with_all_iocs_types() -> None:
    iocs = ExtractedIocs(
        cves=("CVE-2024-1",),
        mitre_techniques=("T1059",),
        mitre_groups=("G0016",),  # group は attack_pattern にしないので bundle に出ない
        ipv4=("198.51.100.1",),
        ipv6=("2001:db8::1",),
        domains=("evil.example.com",),
        urls=("https://evil.example.com/x",),
        md5=("5d41402abc4b2a76b9719d911017c592",),
        sha1=("aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d",),
        sha256=("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",),
    )
    bundle = to_bundle(iocs, [])
    indicator_count = sum(1 for o in bundle["objects"] if o["type"] == "indicator")
    # ipv4 + ipv6 + domain + url + 3 hashes = 7
    assert indicator_count == 7


def test_organization_actor_maps_to_identity() -> None:
    """Actors Stage 5: kind=organization は threat-actor でなく identity。"""
    org = ActorAlias(id="russia_gru", canonical="Russia GRU", kind="organization")
    obj = threat_actor_for(org)
    assert obj["type"] == "identity"
    assert obj["identity_class"] == "organization"


def test_contractor_actor_maps_to_identity() -> None:
    c = ActorAlias(id="i_soon", canonical="i-Soon", kind="contractor")
    assert threat_actor_for(c)["type"] == "identity"


def test_bundle_emits_attributed_to_when_both_present() -> None:
    """group→organization の attributed-to 関係が bundle に入る (両者 present 時)。"""
    iocs = ExtractedIocs(cves=("CVE-2024-1234",))
    actors = [
        ActorAlias(id="russia_gru", canonical="Russia GRU", kind="organization"),
        ActorAlias(id="apt28", canonical="APT28", kind="group", sponsor_org="russia_gru"),
    ]
    bundle = to_bundle(iocs, actors)
    rels = [o for o in bundle["objects"] if o["type"] == "relationship"]
    assert len(rels) == 1
    assert rels[0]["relationship_type"] == "attributed-to"
    ta = next(o for o in bundle["objects"] if o["type"] == "threat-actor")
    ident = next(
        o for o in bundle["objects"] if o["type"] == "identity" and o["name"] == "Russia GRU"
    )
    assert rels[0]["source_ref"] == ta["id"]
    assert rels[0]["target_ref"] == ident["id"]


def test_bundle_no_attributed_to_when_org_absent() -> None:
    """sponsor_org が bundle 内に無ければ dangling 関係を作らない。"""
    iocs = ExtractedIocs(cves=("CVE-2024-1234",))
    actors = [ActorAlias(id="apt28", canonical="APT28", kind="group", sponsor_org="russia_gru")]
    bundle = to_bundle(iocs, actors)
    assert [o for o in bundle["objects"] if o["type"] == "relationship"] == []
