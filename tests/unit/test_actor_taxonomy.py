"""アクターの種類を STIX threat-actor-type-ov で導く (2026-09-27)。

辞書の family 欄は命名系統 (typhoon / panda …) と活動の種類 (ransom_group …) が混在する。
種類は既存の欄 (kind・family・nation・sponsor) から 1 か所で導き、欄を 275 件に書き足さない。
"""

from __future__ import annotations

from dataclasses import dataclass

from src.cti.actor_taxonomy import threat_actor_type


@dataclass(frozen=True)
class _A:
    kind: str = "group"
    family: str | None = None
    nation: str | None = None
    sponsor: str | None = None
    sponsor_org: str | None = None


def test_families_map_to_stix_types() -> None:
    assert threat_actor_type(_A(family="ransom_group", nation="ru")) == "crime-syndicate"
    assert threat_actor_type(_A(family="spider")) == "crime-syndicate"
    assert threat_actor_type(_A(family="hacktivist", nation="ru")) == "activist"
    assert threat_actor_type(_A(family="typhoon", nation="cn")) == "nation-state"
    assert threat_actor_type(_A(family="state_organ")) == "nation-state"


def test_family_less_actors() -> None:
    assert threat_actor_type(_A(nation="cn")) == "nation-state"  # 帰属国つきの APT
    assert threat_actor_type(_A(sponsor_org="russia_gru")) == "nation-state"
    assert threat_actor_type(_A()) == "unknown"


def test_organizations_are_not_threat_actors() -> None:
    assert threat_actor_type(_A(kind="organization", nation="ru")) is None
    assert threat_actor_type(_A(kind="contractor", nation="cn")) is None


def test_real_registry() -> None:
    from src.cti.actor_normalizer import load_actor_aliases

    by_id = {a.id: a for a in load_actor_aliases().actors}
    assert threat_actor_type(by_id["qilin"]) == "crime-syndicate"
    assert threat_actor_type(by_id["apt28"]) == "nation-state"
    assert threat_actor_type(by_id["russia_gru"]) is None
