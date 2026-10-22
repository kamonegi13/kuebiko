"""日本の同盟・同志国の区分と、根拠の「直接 / 間接」の印 (2026-10-22)。

区分の SSoT は ``config/cti/countries.yaml`` の ``alliance: ally`` (国の SSoT に同居させ、
複製辞書を作らない)。``threat_actor_doctrine.ALLIED_NATIONS`` もここから導出する。

常設の問いの根拠は「日本の被害 (直接)」と「世界での同種活動 (間接)」の 2 本腕だが、
同盟国の被害は日本の被害に準じて読む。そこで根拠 1 件を **被害国で** 3 つに分ける:

- direct   = 被害国が日本または同盟・同志国
- indirect = それ以外の国
- unknown  = 被害国が付いていない

印を付けるだけで、答えの重み付けには使わない (旗 ``STANDING_EVIDENCE_PROXIMITY`` は表示・集計の
ON/OFF。0 で印を出さない)。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

from src.logging_config import get_logger

_log = get_logger(__name__)

_COUNTRIES_YAML = Path(__file__).resolve().parents[2] / "config" / "cti" / "countries.yaml"
_ALLY = "ally"
_HOME = "JP"
_FLAG_ENV = "STANDING_EVIDENCE_PROXIMITY"

PROXIMITY_DIRECT = "direct"
PROXIMITY_INDIRECT = "indirect"
PROXIMITY_UNKNOWN = "unknown"
PROXIMITY_LABELS_JA: dict[str, str] = {
    PROXIMITY_DIRECT: "直接 (日本・同盟国)",
    PROXIMITY_INDIRECT: "間接 (それ以外)",
    PROXIMITY_UNKNOWN: "被害国不明",
}


def load_allied_from_yaml(path: Path) -> frozenset[str]:
    """countries.yaml から ``alliance: ally`` の国 (小文字 ISO2) を読む。

    読めないときは空集合 (警告を残す)。呼出側の重み付けが無音で変わるため、ログで追える。
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        _log.warning("alliance_yaml_unreadable", path=str(path), error=str(e))
        return frozenset()
    canonical = data.get("canonical") if isinstance(data, dict) else None
    if not isinstance(canonical, dict):
        return frozenset()
    return frozenset(
        str(iso).lower()
        for iso, body in canonical.items()
        if isinstance(body, dict) and body.get("alliance") == _ALLY
    )


@lru_cache(maxsize=1)
def allied_nations() -> frozenset[str]:
    """日本の同盟・同志国 (小文字 ISO2)。"""
    return load_allied_from_yaml(_COUNTRIES_YAML)


def proximity_enabled() -> bool:
    """旗 ``STANDING_EVIDENCE_PROXIMITY`` (既定 on、0 で印を出さない)。"""
    return os.environ.get(_FLAG_ENV, "1").strip() != "0"


def evidence_proximity(victim_country_iso: str | None) -> str:
    """被害国 → 直接 / 間接 / 不明。"""
    iso = (victim_country_iso or "").strip()
    if not iso:
        return PROXIMITY_UNKNOWN
    if iso.upper() == _HOME or iso.lower() in allied_nations():
        return PROXIMITY_DIRECT
    return PROXIMITY_INDIRECT
