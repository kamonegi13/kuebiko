"""記事 1 件の STIX 2.1 bundle (2026-09-27) — 準拠と、主題/言及の区別。"""

from __future__ import annotations

from typing import Any

from src.cti.actor_normalizer import ActorAlias, ActorAliasRegistry
from src.cti.stix.article import ArticleFacts, build_article_bundle
from src.cti.stix.core import EXTENSION_ID
from tests.unit.stix_validation import assert_extensions_match_schema, assert_valid_stix


def _registry() -> ActorAliasRegistry:
    return ActorAliasRegistry(
        actors=(
            ActorAlias(
                id="apt28",
                canonical="APT28",
                aliases=("Fancy Bear",),
                nation="ru",
                sponsor_org="russia_gru",
                mitre_group="G0007",
            ),
            ActorAlias(id="russia_gru", canonical="Russia GRU", kind="organization", nation="ru"),
            ActorAlias(id="turla", canonical="Turla", nation="ru"),
        )
    )


def _facts(**kw: Any) -> ArticleFacts:
    base: dict[str, Any] = {
        "article_id": "rss:https://kuebiko.example/a1",
        "title": "APT28 が政府機関を標的に",
        "url": "https://kuebiko.example/a1",
        "feed_title": "Example Feed",
        "summary": "要約",
        "created": "2026-09-20T01:02:03.000Z",
        "published": "2026-09-19T00:00:00.000Z",
        "importance": "high",
        "category": "apt",
        "pir_ids": ("pir_russia_apt",),
        "subject_actor_ids": ("apt28",),
        "subject_source": "title",
        "mentioned_actor_ids": ("apt28", "turla"),
        "malware": ("X-Agent",),
        "tools": ("Mimikatz",),
        "ttps": ("T1566.001",),
        "cves": ("CVE-2026-1234",),
        "iocs": (("ipv4", "203.0.113.5"), ("domain", "evil.example"), ("sha256", "a" * 64)),
        "victim_orgs": ("Example Ministry",),
        "victim_country_iso": "jp",
        "victim_sector": "government",
        "intent": "espionage",
    }
    return ArticleFacts(**{**base, **kw})


def _by_type(b: dict[str, Any], t: str) -> list[dict[str, Any]]:
    return [x for x in b["objects"] if x["type"] == t]


def test_article_bundle_is_valid_stix_21() -> None:
    b = build_article_bundle(_facts(), _registry())

    assert_valid_stix(b)
    assert_extensions_match_schema(b)


def test_empty_article_is_still_valid() -> None:
    f = _facts(
        subject_actor_ids=(),
        mentioned_actor_ids=(),
        malware=(),
        tools=(),
        ttps=(),
        cves=(),
        iocs=(),
        victim_orgs=(),
        victim_country_iso="",
        victim_sector="",
        intent="",
    )
    assert_valid_stix(build_article_bundle(f, _registry()))


def test_group_is_intrusion_set_and_organization_is_threat_actor() -> None:
    b = build_article_bundle(_facts(), _registry())

    assert {x["name"] for x in _by_type(b, "intrusion-set")} == {"APT28", "Turla"}
    assert [x["name"] for x in _by_type(b, "threat-actor")] == ["Russia GRU"]
    attributed = [
        r for r in _by_type(b, "relationship") if r["relationship_type"] == "attributed-to"
    ]
    assert len(attributed) == 1


def test_only_subject_actor_gets_relationships() -> None:
    b = build_article_bundle(_facts(), _registry())
    ids = {x["name"]: x["id"] for x in _by_type(b, "intrusion-set")}
    sources = {r["source_ref"] for r in _by_type(b, "relationship")}

    assert ids["APT28"] in sources
    assert ids["Turla"] not in sources  # 言及だけのアクターは手口・被害と結ばない


def test_relationship_confidence_follows_subject_source() -> None:
    title = build_article_bundle(_facts(), _registry())
    llm = build_article_bundle(
        _facts(subject_source="llm", subject_confidence="medium"), _registry()
    )

    def conf(b: dict[str, Any]) -> set[int]:
        return {r["confidence"] for r in _by_type(b, "relationship") if "confidence" in r}

    assert conf(title) == {85}
    assert conf(llm) == {50}


def test_report_carries_kuebiko_extension() -> None:
    b = build_article_bundle(_facts(), _registry())
    ext = _by_type(b, "report")[0]["extensions"][EXTENSION_ID]

    assert ext["extension_type"] == "property-extension"
    assert ext["subject_actor_ids"] == ["apt28"]
    assert ext["pir_ids"] == ["pir_russia_apt"]


def test_same_article_gives_same_ids() -> None:
    a = build_article_bundle(_facts(), _registry())
    b = build_article_bundle(_facts(), _registry())

    assert [x["id"] for x in a["objects"]] == [x["id"] for x in b["objects"]]
