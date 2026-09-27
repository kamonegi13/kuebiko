"""事象どうしの関係の導出 (2026-09-27、src/eventnews/relations.py) — 規則ごとの成立・不成立。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from src.eventnews.relations import EventFeatures, derive_relations

_T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _e(item_id: str, day: int, span: int = 1, **kw: Any) -> EventFeatures:
    first = _T0 + timedelta(days=day)
    fields: dict[str, Any] = {
        k: frozenset(v) if isinstance(v, (set, list, tuple)) else v for k, v in kw.items()
    }
    return EventFeatures(item_id=item_id, first=first, last=first + timedelta(days=span), **fields)


def _nation(actor: str) -> str | None:
    return {"apt_a": "cn", "apt_b": "cn", "unit": "cn", "rus": "ru"}.get(actor)


def _related(x: str, y: str) -> bool:
    return {x, y} == {"apt_a", "unit"}  # 部隊と上位組織


def _derive(*events: EventFeatures) -> dict[tuple[str, str], str]:
    rels = derive_relations(list(events), nation_of=_nation, related_actors=_related)
    return {(r.a, r.b): r.rel_type for r in rels}


def test_same_victim_after_a_gap_is_a_follow_up() -> None:
    got = _derive(_e("a", 0, victims={"acme"}), _e("b", 20, victims={"acme"}))

    assert got == {("a", "b"): "follow_up"}


def test_same_cve_with_different_kinds_is_a_side() -> None:
    got = _derive(
        _e("adv", 0, cves={"CVE-2026-1"}, kinds={"advisory"}),
        _e("exp", 2, cves={"CVE-2026-1"}, kinds={"exploitation"}),
    )

    assert got == {("adv", "exp"): "side"}


def test_same_actor_and_rare_tool_with_other_victims_is_a_campaign() -> None:
    got = _derive(
        _e("a", 0, subjects={"apt_a"}, malware={"rarebot"}, victims={"x"}),
        _e("b", 10, subjects={"apt_a"}, malware={"rarebot"}, victims={"y"}),
    )

    assert got == {("a", "b"): "campaign"}


def test_same_actor_without_shared_capability_is_not_related() -> None:
    got = _derive(_e("a", 0, subjects={"apt_a"}), _e("b", 5, subjects={"apt_a"}))

    assert got == {}


def test_different_actors_of_one_nation_sharing_a_tool_is_a_supplier() -> None:
    got = _derive(
        _e("a", 0, subjects={"apt_a"}, malware={"kit"}),
        _e("b", 30, subjects={"apt_b"}, malware={"kit"}),
    )

    assert got == {("a", "b"): "supplier"}


def test_unit_and_parent_organization_are_not_a_supplier() -> None:
    got = _derive(
        _e("a", 0, subjects={"apt_a"}, malware={"kit"}),
        _e("b", 30, subjects={"unit"}, malware={"kit"}),
    )

    assert got == {}


def test_roundup_contains_the_individual_event() -> None:
    got = _derive(
        _e("weekly", 5, roundup=True, cves={"CVE-2026-9"}), _e("one", 3, cves={"CVE-2026-9"})
    )

    assert got == {("weekly", "one"): "contains"}


def test_common_indicator_does_not_link() -> None:
    """珍しくない (多くの事象に出る) マルウェアは線にしない。"""
    events = [_e(f"e{i}", i, malware={"common"}, sectors={"finance"}) for i in range(12)]

    assert _derive(*events) == {}


def test_actor_names_are_not_counted_as_capabilities() -> None:
    """「LockBit 5.0」「Kimsuky」はアクター名 — 能力の共有に数えない (盲検 2026-09-27)。"""
    from src.eventnews.relation_features import _is_actor_name

    names = frozenset({"lockbit", "kimsuky"})

    assert _is_actor_name("LockBit 5.0", names)
    assert _is_actor_name("Kimsuky", names)
    assert not _is_actor_name("BlackEnergy", names)
