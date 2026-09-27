"""STIX 2.1 の SDO / SRO の組み立て (2026-09-27)。すべて新しい dict を返す (入力は書き換えない)。

kuebiko → STIX の対応 (docs/stix_export.md):
- 辞書の group → ``intrusion-set`` (持続的な活動のまとまり)。機関・請負 → ``threat-actor``
  (``threat_actor_types`` は actor_taxonomy)。group → 機関は ``attributed-to``
- マルウェア → ``malware`` (is_family) / ツール → ``tool`` / TTP → ``attack-pattern``
  (ATT&CK の external_id + kill_chain_phases) / CVE → ``vulnerability``
- 被害組織 → ``identity`` (organization) / 業種 → ``identity`` (class) / 国 → ``location``
- IOC → ``indicator`` (STIX パターン)
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from src.cti.actor_normalizer import ActorAlias
from src.cti.stix.core import REFERENCE_TS, sdo, with_extension

#: kuebiko の IOC 種別 → (STIX パターンの対象, 表示名)
_IOC_PATTERN = {
    "ipv4": ("ipv4-addr:value", "IPv4"),
    "ipv6": ("ipv6-addr:value", "IPv6"),
    "domain": ("domain-name:value", "Domain"),
    "url": ("url:value", "URL"),
    "md5": ("file:hashes.MD5", "MD5"),
    "sha1": ("file:hashes.'SHA-1'", "SHA-1"),
    "sha256": ("file:hashes.'SHA-256'", "SHA-256"),
}

_ORG_KINDS = frozenset({"organization", "contractor"})


def is_org(actor: ActorAlias) -> bool:
    return actor.kind in _ORG_KINDS


def actor_ref(actor: ActorAlias) -> str:
    """アクターの STIX ID (group = intrusion-set / 機関 = threat-actor)。"""
    from src.cti.stix.core import stix_id

    return stix_id("threat-actor" if is_org(actor) else "intrusion-set", actor.id)


def _mitre_group_ref(actor: ActorAlias) -> list[dict[str, str]] | None:
    if not actor.mitre_group:
        return None
    return [
        {
            "source_name": "mitre-attack",
            "external_id": actor.mitre_group,
            "url": f"https://attack.mitre.org/groups/{actor.mitre_group}/",
        }
    ]


def actor_object(actor: ActorAlias, *, primary_motivation: str | None = None) -> dict[str, Any]:
    """辞書のアクター → intrusion-set (group) / threat-actor (機関・請負)。"""
    from src.cti.actor_taxonomy import threat_actor_type

    common = {
        "name": actor.canonical,
        "description": actor.description or None,
        "aliases": list(actor.aliases) or None,
        "external_references": _mitre_group_ref(actor),
    }
    if is_org(actor):
        obj = sdo(
            "threat-actor",
            actor.id,
            _ts=REFERENCE_TS,
            threat_actor_types=["nation-state"] if actor.nation else ["unknown"],
            sophistication="strategic",
            primary_motivation=primary_motivation,
            **common,
        )
        return with_extension(obj, {"actor_id": actor.id, "actor_kind": actor.kind})
    obj = sdo(
        "intrusion-set",
        actor.id,
        _ts=REFERENCE_TS,
        first_seen=None,
        primary_motivation=primary_motivation,
        **common,
    )
    return with_extension(
        obj,
        {
            "actor_id": actor.id,
            "actor_kind": actor.kind,
            "threat_actor_type": threat_actor_type(actor),
            "nation": (actor.nation or "").upper() or None,
        },
    )


_SECTORS_YAML = Path("config/cti/victim_sectors.yaml")


@lru_cache(maxsize=1)
def _sector_table() -> dict[str, tuple[str, str]]:
    """業種 canonical → (表示名, STIX industry-sector-ov)。SSoT は victim_sectors.yaml の stix。"""
    if not _SECTORS_YAML.exists():
        return {}
    raw = yaml.safe_load(_SECTORS_YAML.read_text(encoding="utf-8")) or {}
    out: dict[str, tuple[str, str]] = {}
    for key, spec in (raw.get("canonical") or {}).items():
        if isinstance(spec, dict) and spec.get("stix"):
            out[str(key)] = (str(spec.get("display") or key), str(spec["stix"]))
    return out


def sector_stix(sector_canonical: str) -> tuple[str, str] | None:
    """業種 canonical → (表示名, STIX の業種語彙)。対応が無ければ None。"""
    return _sector_table().get(sector_canonical)


#: kuebiko のマルウェア種別 (malware_aliases.yaml の type 列) → STIX malware-type-ov (2026-09-27)。
#: infostealer は STIX に語彙が無く、最も近い spyware に写す
_MALWARE_TYPE_TO_STIX = {
    "ransomware": "ransomware",
    "rat": "remote-access-trojan",
    "infostealer": "spyware",
    "backdoor": "backdoor",
    "botnet": "bot",
    "loader": "downloader",
    "worm": "worm",
    "keylogger": "keylogger",
    "spyware": "spyware",
    "wiper": "wiper",
    "unknown": "unknown",
}


def malware_types_of(name: str) -> list[str] | None:
    """マルウェア辞書の種別を STIX の語彙で (辞書に無い・種別未宣言は None)。"""
    from src.cti.malware_normalizer import load_malware_normalizer

    kind = load_malware_normalizer().type_of(name.strip())
    stix = _MALWARE_TYPE_TO_STIX.get(kind or "")
    return [stix] if stix else None


def malware_object(name: str) -> dict[str, Any]:
    return sdo(
        "malware",
        name.strip().lower(),
        _ts=REFERENCE_TS,
        name=name.strip(),
        is_family=True,
        malware_types=malware_types_of(name),
    )


def tool_object(name: str) -> dict[str, Any]:
    return sdo("tool", name.strip().lower(), _ts=REFERENCE_TS, name=name.strip())


def attack_pattern_object(technique_id: str) -> dict[str, Any]:
    """ATT&CK の技術 → attack-pattern (名前・戦術は辞書があれば埋める)。"""
    from src.cti import attack_techniques

    tid = technique_id.strip().upper()
    info = attack_techniques.load_technique_catalog().get(tid)
    url_path = tid.replace(".", "/")
    return sdo(
        "attack-pattern",
        tid,
        _ts=REFERENCE_TS,
        name=f"{tid} {info.name}" if info else tid,
        external_references=[
            {
                "source_name": "mitre-attack",
                "external_id": tid,
                "url": f"https://attack.mitre.org/techniques/{url_path}/",
            }
        ],
        kill_chain_phases=[
            {"kill_chain_name": "mitre-attack", "phase_name": t} for t in info.tactics
        ]
        if info and info.tactics
        else None,
    )


def vulnerability_object(cve: str) -> dict[str, Any]:
    cid = cve.strip().upper()
    return sdo(
        "vulnerability",
        cid,
        _ts=REFERENCE_TS,
        name=cid,
        external_references=[
            {
                "source_name": "cve",
                "external_id": cid,
                "url": f"https://nvd.nist.gov/vuln/detail/{cid}",
            }
        ],
    )


def victim_org_object(name: str, *, sectors: list[str] | None = None) -> dict[str, Any]:
    """被害組織 → identity。ID の鍵は事象の群化と同じ正規化 (表記揺れを畳む) — 同じ組織を
    記事ごとに別の identity に割らない (join_entity_key と同じ normalize_for_match)。"""
    from src.assessment.evidence_verify import normalize_for_match

    return sdo(
        "identity",
        f"victim|{normalize_for_match(name)}",
        _ts=REFERENCE_TS,
        name=name.strip(),
        identity_class="organization",
        sectors=sectors or None,
    )


def sector_object(display: str, stix_sector: str) -> dict[str, Any]:
    return sdo(
        "identity",
        f"sector|{stix_sector}",
        _ts=REFERENCE_TS,
        name=display,
        identity_class="class",
        sectors=[stix_sector],
    )


def location_object(country_iso: str) -> dict[str, Any]:
    iso = country_iso.strip().upper()
    return sdo("location", f"country|{iso}", _ts=REFERENCE_TS, name=iso, country=iso)


def indicator_object(kind: str, value: str, *, valid_from: str) -> dict[str, Any] | None:
    """IOC → indicator。未知の種別は None。"""
    spec = _IOC_PATTERN.get(kind)
    if spec is None:
        return None
    target, label = spec
    safe = value.replace("\\", "\\\\").replace("'", "\\'")
    return sdo(
        "indicator",
        f"{kind}|{value}",
        name=f"{label} {value[:80]}",
        description=f"kuebiko が記事の本文から抽出した {label} の指標",
        indicator_types=["malicious-activity"],
        pattern=f"[{target} = '{safe}']",
        pattern_type="stix",
        valid_from=valid_from,
    )


def relationship(
    source_ref: str,
    rel_type: str,
    target_ref: str,
    *,
    context: str,
    created: str,
    confidence: int | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """関係。``context`` (記事 id・台帳 id 等) を ID に含める — 同じ組でも主張した文脈ごとに
    別の関係にする (同じ ID で確度や日時が違う版を作らない)。"""
    return sdo(
        "relationship",
        f"{rel_type}|{source_ref}->{target_ref}|{context}",
        _ts=created,
        relationship_type=rel_type,
        source_ref=source_ref,
        target_ref=target_ref,
        confidence=confidence,
        description=description,
    )
