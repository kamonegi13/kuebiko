"""STIX 2.1 の土台 — ID・時刻・作成者・マーキング・kuebiko 拡張・確度 (2026-09-27)。

⭐ ID は uuid5 で **決定論的** — 同じ入力なら同じ ID になり、受け手 (OpenCTI / MISP 等) で
重複が畳まれる。SDO の ID を決める鍵は「その対象の同一性」(アクター id・CVE 番号・記事 id)。

kuebiko 独自の情報 (主題/言及・確度の経路・SIR・重要度・台帳の型 等) は
STIX 2.1 の正式な拡張 (extension-definition, property-extension) に入れる。
``x_`` 始まりの独自プロパティ (2.0 流) は使わない — 2.1 で非推奨。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

SPEC_VERSION = "2.1"

#: 決定論 ID の名前空間 (旧 stix_exporter から引き継いだ値)
NAMESPACE = uuid.UUID("8e1f2a4d-3c5b-4a8e-9f01-c7188e1f1abc")

PRODUCER_NAME = "kuebiko"
#: 参照系オブジェクト (作成者・拡張定義・アクター・マルウェア・技術・CVE・組織・国) の created。
#: ⚠ ID が決定論なので、同じ ID の created が書き出しごとに変わると STIX の版の規則
#: (同じ ID の版は created が同じ) に反する。参照系は固定値にする
REFERENCE_TS = "2026-01-01T00:00:00.000Z"
_FIXED_TS = REFERENCE_TS

#: STIX 2.1 仕様 §7.2.1.4 の定義済み TLP。書き出しは公開情報の要約なので TLP:WHITE
TLP_WHITE_ID = "marking-definition--613f2e26-407d-48c7-9eca-b8e91df99dc9"
TLP_WHITE: dict[str, Any] = {
    "type": "marking-definition",
    "spec_version": SPEC_VERSION,
    "id": TLP_WHITE_ID,
    "created": "2017-01-20T00:00:00.000Z",
    "definition_type": "tlp",
    "name": "TLP:WHITE",
    "definition": {"tlp": "white"},
}

#: kuebiko 拡張の JSON Schema (公開リポの docs/stix/)
EXTENSION_SCHEMA_URL = (
    "https://github.com/kamonegi13/kuebiko/blob/main/docs/stix/kuebiko-extension.schema.json"
)
EXTENSION_VERSION = "1.0.0"


def stix_id(obj_type: str, key: str) -> str:
    """``<type>--<UUIDv4 形式>`` (決定論)。

    STIX 2.1 は SDO / SRO の ID に UUIDv4 を推奨する (§2.9)。重複を畳むために決定論にしたいので、
    uuid5 のハッシュを取り、version / variant のビットだけ v4 の形に整える
    (中身は乱数でなくハッシュ — 受け手から見て有効な v4 で、同じ入力なら同じ ID)。
    """
    digest = uuid.uuid5(NAMESPACE, f"{obj_type}|{key}").bytes
    return f"{obj_type}--{uuid.UUID(bytes=digest, version=4)}"


def now_ts() -> str:
    """STIX の timestamp (RFC 3339, UTC, ミリ秒)。"""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z"


def to_ts(value: object) -> str | None:
    """DB の日時 (datetime / ISO 文字列) → STIX の timestamp。読めなければ None。"""
    if value is None or value == "":
        return None
    try:
        dt = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


PRODUCER_ID = stix_id("identity", PRODUCER_NAME)
EXTENSION_ID = stix_id("extension-definition", "kuebiko-property-extension")


def producer_identity() -> dict[str, Any]:
    return {
        "type": "identity",
        "spec_version": SPEC_VERSION,
        "id": PRODUCER_ID,
        "created": _FIXED_TS,
        "modified": _FIXED_TS,
        "name": PRODUCER_NAME,
        "description": "Auto-generated STIX 2.1 content by kuebiko (CTI briefing pipeline)",
        "identity_class": "system",
        "object_marking_refs": [TLP_WHITE_ID],
    }


def extension_definition() -> dict[str, Any]:
    """kuebiko の property-extension の定義 (bundle に 1 つ入れる)。"""
    return {
        "type": "extension-definition",
        "spec_version": SPEC_VERSION,
        "id": EXTENSION_ID,
        "created_by_ref": PRODUCER_ID,
        "created": _FIXED_TS,
        "modified": _FIXED_TS,
        "name": "kuebiko analysis properties",
        "description": (
            "kuebiko が付ける分析の属性 (主題/言及の区別と判定経路・確度、SIR、重要度、"
            "台帳の型、事象の種別 等)。STIX の標準語彙に無いものだけを入れる。"
        ),
        "schema": EXTENSION_SCHEMA_URL,
        "version": EXTENSION_VERSION,
        "extension_types": ["property-extension"],
        "object_marking_refs": [TLP_WHITE_ID],
    }


def with_extension(obj: dict[str, Any], props: dict[str, Any]) -> dict[str, Any]:
    """``obj`` に kuebiko 拡張の値を足した **新しい** dict (空・None の値は落とす)。"""
    clean = {k: v for k, v in props.items() if v not in (None, "", [], {})}
    if not clean:
        return dict(obj)
    ext = {EXTENSION_ID: {"extension_type": "property-extension", **clean}}
    return {**obj, "extensions": ext}


def sdo(obj_type: str, key: str, **props: Any) -> dict[str, Any]:
    """SDO / SRO の共通部分 (作成者・TLP・時刻) つきの dict。``props`` の None は落とす。"""
    ts = props.pop("_ts", None) or now_ts()
    base: dict[str, Any] = {
        "type": obj_type,
        "spec_version": SPEC_VERSION,
        "id": stix_id(obj_type, key),
        "created": ts,
        "modified": ts,
        "created_by_ref": PRODUCER_ID,
        "object_marking_refs": [TLP_WHITE_ID],
    }
    return {**base, **{k: v for k, v in props.items() if v not in (None, "", [])}}


#: 確度の写像 — STIX 2.1 仕様 Appendix A「High-Medium-Low」尺度
_HML_TO_CONFIDENCE = {"high": 85, "medium": 50, "moderate": 50, "low": 15, "none": 0}


def confidence_value(level: str | None) -> int | None:
    """kuebiko の確度 (high / medium / moderate / low) → STIX の confidence (0-100)。"""
    if not level:
        return None
    return _HML_TO_CONFIDENCE.get(level.strip().lower())


def bundle(objects: list[dict[str, Any]], *, key: str) -> dict[str, Any]:
    """作成者・TLP・拡張定義を先頭に置き、ID の重複を除いた bundle。"""
    head = [TLP_WHITE, producer_identity(), extension_definition()]
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for o in [*head, *objects]:
        if o["id"] in seen:
            continue
        seen.add(o["id"])
        out.append(o)
    return {"type": "bundle", "id": stix_id("bundle", key), "objects": out}
