"""BriefingMessage / IOC / アクターを STIX 2.1 Bundle に変換する (Phase 4)。

STIX 2.1 は OASIS が標準化した CTI 共有のための JSON スキーマ。
TIP (Threat Intelligence Platform) や MISP、OpenCTI 等の他システムへの
連携のためにエクスポート機能を提供する。

参考:
    - https://docs.oasis-open.org/cti/stix/v2.1/cs02/stix-v2.1-cs02.html

サポートする STIX オブジェクト:
    - bundle (root)
    - indicator (CVE, IPv4, domain, URL, file:hashes)
    - threat-actor (canonical name + aliases)
    - vulnerability (CVE)
    - relationship (関係性、簡易版)

外部依存を増やさないため hand-rolled JSON 生成 (stix2 ライブラリ不使用)。
ID は uuid5 で **決定論的** に生成 → 同じ入力なら同じ ID で
他システム側の重複検出が機能する。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from src.cti.actor_normalizer import ActorAlias
from src.cti.diamond_model import intent_to_stix_motivation
from src.cti.ioc_extractor import ExtractedIocs

# Phase 4 で発行する STIX オブジェクトの identity (このシステム自身の名前空間)
# 固定 uuid4 を namespace として使い、入力 string と組み合わせて uuid5 で
# 決定論的な ID を生成する。
_NAMESPACE_UUID = uuid.UUID("8e1f2a4d-3c5b-4a8e-9f01-c7188e1f1abc")

_PRODUCER_NAME = "kuebiko"
_PRODUCER_DESCRIPTION = "Auto-generated STIX 2.1 bundle by kuebiko"

# STIX 2.1 仕様
_STIX_VERSION = "2.1"


def _now_isoformat() -> str:
    """STIX 仕様の timestamp 形式 (RFC 3339, UTC, ms 精度)。"""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _stix_id(obj_type: str, key: str) -> str:
    """STIX ID を uuid5 で決定論的に生成する。"""
    u = uuid.uuid5(_NAMESPACE_UUID, f"{obj_type}|{key}")
    return f"{obj_type}--{u}"


def _producer_identity_id() -> str:
    """このパイプライン自身の identity ID。"""
    return _stix_id("identity", _PRODUCER_NAME)


def _producer_identity() -> dict[str, Any]:
    return {
        "type": "identity",
        "spec_version": _STIX_VERSION,
        "id": _producer_identity_id(),
        "created": "2026-01-01T00:00:00.000Z",  # 固定 (常に同じ identity)
        "modified": "2026-01-01T00:00:00.000Z",
        "name": _PRODUCER_NAME,
        "description": _PRODUCER_DESCRIPTION,
        "identity_class": "system",
    }


def indicator_for_ipv4(ip: str) -> dict[str, Any]:
    return _indicator(
        pattern=f"[ipv4-addr:value = '{ip}']",
        pattern_type="stix",
        types=["malicious-activity"],
        name=f"IPv4 {ip}",
        key=f"ipv4|{ip}",
    )


def indicator_for_ipv6(ip: str) -> dict[str, Any]:
    return _indicator(
        pattern=f"[ipv6-addr:value = '{ip}']",
        pattern_type="stix",
        types=["malicious-activity"],
        name=f"IPv6 {ip}",
        key=f"ipv6|{ip}",
    )


def indicator_for_domain(domain: str) -> dict[str, Any]:
    return _indicator(
        pattern=f"[domain-name:value = '{domain}']",
        pattern_type="stix",
        types=["malicious-activity"],
        name=f"Domain {domain}",
        key=f"domain|{domain}",
    )


def indicator_for_url(url: str) -> dict[str, Any]:
    # シングルクォートエスケープ (STIX 仕様)
    safe_url = url.replace("'", "\\'")
    return _indicator(
        pattern=f"[url:value = '{safe_url}']",
        pattern_type="stix",
        types=["malicious-activity"],
        name=f"URL {url[:80]}",
        key=f"url|{url}",
    )


def indicator_for_hash(hash_value: str, algorithm: str) -> dict[str, Any]:
    """algorithm: "MD5" / "SHA-1" / "SHA-256"."""
    return _indicator(
        pattern=f"[file:hashes.'{algorithm}' = '{hash_value}']",
        pattern_type="stix",
        types=["malicious-activity"],
        name=f"{algorithm} {hash_value[:16]}...",
        key=f"hash|{algorithm}|{hash_value}",
    )


def vulnerability_for_cve(cve: str) -> dict[str, Any]:
    """CVE は STIX vulnerability オブジェクトで表現する。"""
    return {
        "type": "vulnerability",
        "spec_version": _STIX_VERSION,
        "id": _stix_id("vulnerability", cve.upper()),
        "created": _now_isoformat(),
        "modified": _now_isoformat(),
        "created_by_ref": _producer_identity_id(),
        "name": cve.upper(),
        "external_references": [
            {
                "source_name": "cve",
                "external_id": cve.upper(),
                "url": f"https://nvd.nist.gov/vuln/detail/{cve.upper()}",
            },
        ],
    }


def threat_actor_for(
    actor: ActorAlias,
    *,
    primary_motivation: str | None = None,
) -> dict[str, Any]:
    """エイリアス辞書のアクターを STIX threat-actor に変換。

    Phase Diamond-Axes: ``primary_motivation`` (STIX ``attack-motivation-ov`` 値)
    を渡すと threat-actor.primary_motivation に設定する。これは Diamond Model の
    socio-political 軸 (Adversary⇄Victim の意図) を STIX 標準語彙へ写像したもの。

    Actors Stage 5: kind=organization/contractor は threat-actor でなく
    identity (class=organization) に写像する。国家情報機関そのものを threat-actor に
    すると「機関 = 単一の攻撃実行主体」という誤った意味論になるため。配下グループとの
    関係は ``attributed_to_for`` の attributed-to relationship で表現する。
    """
    if actor.kind in ("organization", "contractor"):
        ident: dict[str, Any] = {
            "type": "identity",
            "spec_version": _STIX_VERSION,
            "id": _stix_id("identity", actor.id),
            "created": _now_isoformat(),
            "modified": _now_isoformat(),
            "created_by_ref": _producer_identity_id(),
            "name": actor.canonical,
            "identity_class": "organization",
        }
        if actor.description:
            ident["description"] = actor.description
        return ident

    obj: dict[str, Any] = {
        "type": "threat-actor",
        "spec_version": _STIX_VERSION,
        "id": _stix_id("threat-actor", actor.id),
        "created": _now_isoformat(),
        "modified": _now_isoformat(),
        "created_by_ref": _producer_identity_id(),
        "name": actor.canonical,
    }
    if actor.aliases:
        obj["aliases"] = list(actor.aliases)
    if actor.description:
        obj["description"] = actor.description
    if primary_motivation:
        obj["primary_motivation"] = primary_motivation
    if actor.sponsor:
        obj["sophistication"] = "strategic"  # 国家系想定
    if actor.mitre_group:
        obj["external_references"] = [
            {
                "source_name": "mitre-attack",
                "external_id": actor.mitre_group,
                "url": f"https://attack.mitre.org/groups/{actor.mitre_group}/",
            },
        ]
    return obj


def _stix_actor_ref(actor_id: str, *, is_org: bool) -> str:
    """actor id → STIX ref。organization は identity、それ以外は threat-actor。"""
    return _stix_id("identity" if is_org else "threat-actor", actor_id)


def attributed_to_for(group: ActorAlias) -> dict[str, Any] | None:
    """kind=group で sponsor_org があれば threat-actor → identity の attributed-to 関係。"""
    if group.kind != "group" or not group.sponsor_org:
        return None
    return {
        "type": "relationship",
        "spec_version": _STIX_VERSION,
        "id": _stix_id("relationship", f"attributed-to|{group.id}->{group.sponsor_org}"),
        "created": _now_isoformat(),
        "modified": _now_isoformat(),
        "created_by_ref": _producer_identity_id(),
        "relationship_type": "attributed-to",
        "source_ref": _stix_actor_ref(group.id, is_org=False),
        "target_ref": _stix_actor_ref(group.sponsor_org, is_org=True),
    }


def attack_pattern_for_technique(technique: str) -> dict[str, Any]:
    """MITRE ATT&CK Technique を STIX attack-pattern に変換。"""
    norm = technique.upper().strip()
    return {
        "type": "attack-pattern",
        "spec_version": _STIX_VERSION,
        "id": _stix_id("attack-pattern", norm),
        "created": _now_isoformat(),
        "modified": _now_isoformat(),
        "created_by_ref": _producer_identity_id(),
        "name": f"MITRE ATT&CK {norm}",
        "external_references": [
            {
                "source_name": "mitre-attack",
                "external_id": norm,
                "url": (f"https://attack.mitre.org/techniques/{norm.replace('.', '/')}/"),
            },
        ],
    }


def to_bundle(
    iocs: ExtractedIocs,
    actors: list[ActorAlias] | None = None,
    *,
    description: str = "",
    socio_political_intent: str | None = None,
) -> dict[str, Any]:
    """IOC + アクターから STIX 2.1 Bundle を組み立てる。

    Args:
        iocs: 機械抽出した IOC コレクション
        actors: 関連する脅威アクターのリスト (空可)
        description: bundle に付与する説明 (note オブジェクトに格納)
        socio_political_intent: Diamond Model socio-political 軸の canonical intent。
            STIX ``attack-motivation-ov`` に写像して threat-actor.primary_motivation
            に付与する (該当なし / unknown は無視)。

    Returns:
        STIX 2.1 Bundle JSON 互換 dict
    """
    motivation = intent_to_stix_motivation(socio_political_intent or "")
    objects: list[dict[str, Any]] = [_producer_identity()]

    # CVE → vulnerability
    for cve in iocs.cves:
        objects.append(vulnerability_for_cve(cve))

    # IPv4 / IPv6 / Domain / URL → indicator
    for ip in iocs.ipv4:
        objects.append(indicator_for_ipv4(ip))
    for ip in iocs.ipv6:
        objects.append(indicator_for_ipv6(ip))
    for d in iocs.domains:
        objects.append(indicator_for_domain(d))
    for u in iocs.urls:
        objects.append(indicator_for_url(u))

    # Hash → indicator
    for h in iocs.md5:
        objects.append(indicator_for_hash(h, "MD5"))
    for h in iocs.sha1:
        objects.append(indicator_for_hash(h, "SHA-1"))
    for h in iocs.sha256:
        objects.append(indicator_for_hash(h, "SHA-256"))

    # MITRE Technique → attack-pattern
    for tech in iocs.mitre_techniques:
        objects.append(attack_pattern_for_technique(tech))

    # Actor → threat-actor / identity (socio-political intent → primary_motivation)
    actor_list = list(actors or [])
    present_ids = {a.id for a in actor_list}
    for actor in actor_list:
        objects.append(threat_actor_for(actor, primary_motivation=motivation))
    # group → 親 organization の attributed-to (両者が bundle 内にある場合のみ)
    for actor in actor_list:
        rel = attributed_to_for(actor)
        if rel is not None and actor.sponsor_org in present_ids:
            objects.append(rel)

    # 全体に description note を 1 つ
    if description:
        objects.append(
            {
                "type": "note",
                "spec_version": _STIX_VERSION,
                "id": _stix_id(
                    "note",
                    f"{description}|{_now_isoformat()}",
                ),
                "created": _now_isoformat(),
                "modified": _now_isoformat(),
                "created_by_ref": _producer_identity_id(),
                "abstract": description[:80],
                "content": description,
                "object_refs": [o["id"] for o in objects if o["type"] != "note"],
            },
        )

    return {
        "type": "bundle",
        "id": _stix_id(
            "bundle",
            f"{description}|{_now_isoformat()}",
        ),
        "objects": objects,
    }


# ---------- internal ----------


def _indicator(
    *,
    pattern: str,
    pattern_type: str,
    types: list[str],
    name: str,
    key: str,
) -> dict[str, Any]:
    return {
        "type": "indicator",
        "spec_version": _STIX_VERSION,
        "id": _stix_id("indicator", key),
        "created": _now_isoformat(),
        "modified": _now_isoformat(),
        "created_by_ref": _producer_identity_id(),
        "name": name,
        "pattern": pattern,
        "pattern_type": pattern_type,
        "valid_from": _now_isoformat(),
        "indicator_types": types,
    }
