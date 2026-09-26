"""ATT&CK の技術 → 名前・戦術 (kill chain)・親技術の辞書 (2026-09-27)。

記事の TTP (article_entities.ttp) は ID だけで、名前・戦術・親技術との関係が無かった。
MITRE の STIX データ (mitre_sync が週次で取得) から作り、
``data/cti/attack_techniques.json`` に置く。
⚠ data/ に置く (公開リポに MITRE のデータを複製しない)。無ければ空の辞書で縮退する。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.logging_config import get_logger

_log = get_logger(__name__)

DEFAULT_CATALOG_PATH = Path("data/cti/attack_techniques.json")


@dataclass(frozen=True)
class TechniqueInfo:
    technique_id: str
    name: str
    tactics: tuple[str, ...]  # kill chain の phase_name (initial-access 等)
    parent: str | None  # 副技術 (T1566.001) の親 (T1566)。本体は None
    # 取り消された ID の置き換え先 (記事には古い版の ID が残る: T1562.001 → T1685 等)
    replaced_by: str | None = None


def _mitre_id(obj: dict[str, Any]) -> str | None:
    for ref in obj.get("external_references", []) or []:
        if ref.get("source_name") == "mitre-attack" and ref.get("external_id"):
            return str(ref["external_id"])
    return None


def parse_technique_catalog(objects: list[dict[str, Any]]) -> dict[str, TechniqueInfo]:
    """STIX bundle の objects から技術の辞書を作る (pure)。

    廃止 (deprecated) は除く。取り消し (revoked) は revoked-by の置き換え先があれば、
    置き換え先の名前・戦術で ``replaced_by`` つきの項目にする (記事の古い ID を読めるように)。
    """
    by_stix: dict[str, dict[str, Any]] = {o["id"]: o for o in objects if "id" in o}
    revoked_by = {
        o.get("source_ref", ""): o.get("target_ref", "")
        for o in objects
        if o.get("type") == "relationship" and o.get("relationship_type") == "revoked-by"
    }
    out: dict[str, TechniqueInfo] = {}
    revoked: list[tuple[str, str]] = []  # (古い ID, 置き換え先の stix id)
    for o in objects:
        if o.get("type") != "attack-pattern" or o.get("x_mitre_deprecated"):
            continue
        tid = _mitre_id(o)
        if not tid:
            continue
        if o.get("revoked"):
            if o["id"] in revoked_by:
                revoked.append((tid, revoked_by[o["id"]]))
            continue
        tactics = tuple(
            str(p.get("phase_name"))
            for p in o.get("kill_chain_phases", []) or []
            if p.get("kill_chain_name") == "mitre-attack" and p.get("phase_name")
        )
        parent = tid.split(".", 1)[0] if "." in tid else None
        out[tid] = TechniqueInfo(tid, str(o.get("name", "")), tactics, parent)
    for old, target in revoked:
        new_id = _mitre_id(by_stix.get(target, {}))
        cur = out.get(new_id or "")
        if cur is not None and old not in out:
            out[old] = TechniqueInfo(
                old, cur.name, cur.tactics, cur.parent, replaced_by=cur.technique_id
            )
    return out


def save_technique_catalog(
    catalog: dict[str, TechniqueInfo], path: Path = DEFAULT_CATALOG_PATH
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({k: asdict(v) for k, v in sorted(catalog.items())}, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(path)  # atomic
    load_technique_catalog.cache_clear()


@lru_cache(maxsize=2)
def load_technique_catalog(path: str = str(DEFAULT_CATALOG_PATH)) -> dict[str, TechniqueInfo]:
    """辞書を読む。無い・壊れているときは空 (呼び手は ID だけで縮退する)。"""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _log.warning("attack_technique_catalog_unreadable", error=str(e)[:120])
        return {}
    return {
        k: TechniqueInfo(
            v["technique_id"], v["name"], tuple(v["tactics"]), v.get("parent"), v.get("replaced_by")
        )
        for k, v in raw.items()
    }
