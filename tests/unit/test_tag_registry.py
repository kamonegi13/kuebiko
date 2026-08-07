"""tag_registry.yaml が現実のタグ (entity_type) を網羅しているかの回帰 (drift 検知)。

レジストリを SSoT として保つため、コード/DB に存在する entity_type が registry に宣言
されていることを検証する。新 entity_type を追加して registry に載せ忘れたら落ちる。
"""

from __future__ import annotations

from pathlib import Path

import yaml

# article_entities に実在する entity_type (main.py _persist_article_entities / ioc 分類 /
# pir evaluator / actor recall / mention_tagger)。新 type を足したらここと registry の両方に載せる。
KNOWN_ENTITY_TYPES = {
    "actor",
    "actor_provisional",
    "campaign",
    "malware_family",
    "tool",
    "ttp",
    "cve",
    "ioc_domain",
    "ioc_ip",
    "ioc_url",
    "ioc_md5",
    "ioc_sha1",
    "ioc_sha256",
    "victim_org",
    "victim_city",
    "involved_country",
    "mentioned_country",
    "pir",
    "malware_type",
    "affected_vendor",
    "affected_product",
}


def _registry_tag_names() -> set[str]:
    raw = yaml.safe_load(Path("config/tag_registry.yaml").read_text(encoding="utf-8"))
    names: set[str] = set()
    for layer in ("envelope", "threat_core", "strategic"):
        names |= set((raw.get(layer) or {}).keys())
    return names


def test_registry_parses() -> None:
    names = _registry_tag_names()
    assert len(names) > 20  # 3 層に全タグが宣言されている


def test_registry_covers_all_entity_types() -> None:
    declared = _registry_tag_names()
    missing = KNOWN_ENTITY_TYPES - declared
    assert not missing, f"tag_registry.yaml に未宣言の entity_type: {missing}"
