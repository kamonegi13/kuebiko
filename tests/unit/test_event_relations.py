"""事象どうしの関係の導出 (2026-09-27、src/eventnews/relations.py) — 規則ごとの成立・不成立。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

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


def test_different_actors_of_one_nation_sharing_a_tool_is_a_supplier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 既定 off (盲検 0/4)。規則そのものは残してテストで固定する
    monkeypatch.setattr("src.eventnews.relations.SUPPLIER_ENABLED", True)
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


class _FakeModel:
    """候補の順に決めた確率を返す分類器 (特徴量は見ない)。"""

    threshold = 0.8

    def __init__(self, probs: list[float]) -> None:
        self._probs = probs

    def probabilities(self, x: Any) -> Any:
        import numpy as np

        return np.array(self._probs)


def _derive_model(events: list[EventFeatures], model: Any) -> dict[tuple[str, str], str]:
    from src.eventnews.relations import derive_with_model

    rels = derive_with_model(
        events, model=model, centroids={}, nation_of=_nation, related_actors=_related
    )
    return {(r.a, r.b): r.rel_type for r in rels}


def test_classifier_decides_incident_and_rule_gives_the_type() -> None:
    events = [_e("a", 0, victims={"acme"}), _e("b", 20, victims={"acme"})]
    model = _FakeModel([0.9])

    assert _derive_model(events, model) == {("a", "b"): "follow_up"}


def test_below_threshold_without_shared_actor_is_not_shown() -> None:
    events = [_e("a", 0, victims={"acme"}), _e("b", 20, victims={"acme"})]
    model = _FakeModel([0.3])

    assert _derive_model(events, model) == {}


def test_shared_trusted_actor_is_same_actor_even_without_model() -> None:
    events = [_e("a", 0, subjects={"apt_a"}), _e("b", 5, subjects={"apt_a"})]

    assert _derive_model(events, None) == {("a", "b"): "same_actor"}


def test_incident_wins_over_same_actor() -> None:
    events = [
        _e("a", 0, subjects={"apt_a"}, cves={"CVE-1"}),
        _e("b", 1, subjects={"apt_a"}, cves={"CVE-1"}),
    ]
    model = _FakeModel([0.95])

    assert _derive_model(events, model) == {("a", "b"): "incident"}


def test_same_actor_is_capped_per_event_by_closeness() -> None:
    from src.eventnews.relations import SAME_ACTOR_CAP, DerivedRelation, index_relations

    rels = [
        DerivedRelation("hub", f"e{i}", "same_actor", ("actor:x",), {"cos": f"{i / 10:.1f}"})
        for i in range(8)
    ] + [DerivedRelation("hub", "inc", "incident", (), {"p": "0.9"})]

    got = index_relations(rels)["hub"]

    assert got[0].b == "inc"  # 同じ出来事の関連が先
    actors = [r.b for r in got if r.rel_type == "same_actor"]
    assert len(actors) == SAME_ACTOR_CAP
    assert actors[0] == "e7"  # 近い順


def test_prolific_actor_still_pairs_nearby_events() -> None:
    """主題の事象が上限を超えるアクターも、時期の近い事象どうしは同じアクターの候補になる。"""
    from src.eventnews.relations import _INDEX_CAP

    events = [_e(f"e{i:03d}", i % 60, subjects={"apt_a"}) for i in range(_INDEX_CAP + 5)]

    got = _derive_model(events, None)

    assert got  # 以前は上限超過で丸ごと飛ばしていた
    assert set(got.values()) == {"same_actor"}
    assert len(got) < len(events) * 11  # 総当たりにはしない


def test_incident_relations_are_capped_per_event() -> None:
    from src.eventnews.relations import INCIDENT_CAP, DerivedRelation, index_relations

    rels = [
        DerivedRelation("hub", f"e{i}", "incident", (), {"p": f"{0.8 + i / 100:.2f}"})
        for i in range(INCIDENT_CAP + 3)
    ]

    got = index_relations(rels)["hub"]

    assert len(got) == INCIDENT_CAP
    assert got[0].b == f"e{INCIDENT_CAP + 2}"  # 確率の高い順


# ---------- GraphRAG 専用の候補線 (relations.GRAPHRAG_EXTRA_TYPES、既定で節に出さない) ----------


def _is_state(nation: str | None) -> Callable[[str], bool]:
    mapping = {"apt_a": True, "apt_b": True, "unit": True, "rus": True, "crime": False}

    def f(actor_id: str) -> bool:
        return mapping.get(actor_id, False)

    return f


def _derive_extra(*events: EventFeatures) -> dict[tuple[str, str], str]:
    from src.eventnews.relations import derive_graphrag_extra_relations

    rels = derive_graphrag_extra_relations(
        list(events), nation_of=_nation, is_state_actor_id=_is_state(None)
    )
    return {(r.a, r.b): r.rel_type for r in rels}


def test_rare_shared_tool_without_shared_subject_is_same_capability() -> None:
    got = _derive_extra(
        _e("a", 0, tools={"rareimplant"}, subjects={"apt_a"}),
        _e("b", 5, tools={"rareimplant"}, subjects={"rus"}),
    )

    assert got == {("a", "b"): "same_capability"}


def test_same_nation_takes_precedence_over_same_capability() -> None:
    """同一能力は節に出さない (精度 3/20) ので、先に当てて同じ帰属国の線を隠さない。"""
    got = _derive_extra(
        _e("a", 0, tools={"rareimplant"}, subjects={"apt_a"}),
        _e("b", 5, tools={"rareimplant"}, subjects={"apt_b"}),
    )

    assert got == {("a", "b"): "same_nation"}


def test_index_extra_relations_keeps_only_enabled_types_and_latest_within_cap() -> None:
    from src.eventnews.relations import DerivedRelation, index_extra_relations

    rels = [DerivedRelation("a", f"b{i}", "same_nation", ("nation:cn",)) for i in range(5)]
    rels.append(DerivedRelation("a", "c", "same_capability", ("cap:x",)))

    got = index_extra_relations(rels, types=frozenset({"same_nation"}), cap=3)

    assert [r.b for r in got["a"]] == ["b2", "b3", "b4"]
    assert "c" not in got


def test_shared_subject_is_not_same_capability() -> None:
    """主題アクターを共有する組は、既存の campaign 規則の対象であって same_capability ではない。"""
    got = _derive_extra(
        _e("a", 0, tools={"rareimplant"}, subjects={"apt_a"}),
        _e("b", 5, tools={"rareimplant"}, subjects={"apt_a"}),
    )

    assert got == {}


def test_different_state_actors_same_nation_is_same_nation() -> None:
    got = _derive_extra(
        _e("a", 0, subjects={"apt_a"}),
        _e("b", 5, subjects={"unit"}),
    )

    assert got == {("a", "b"): "same_nation"}


def test_non_state_actor_is_not_same_nation() -> None:
    got = _derive_extra(
        _e("a", 0, subjects={"apt_a"}),
        _e("b", 5, subjects={"crime"}),
    )

    assert got == {}


def test_rare_same_sector_and_country_within_window_is_same_target() -> None:
    got = _derive_extra(
        _e("a", 0, sectors={"finance"}, countries={"JP"}),
        _e("b", 10, sectors={"finance"}, countries={"JP"}),
    )

    assert got == {("a", "b"): "same_target"}


def test_same_target_outside_window_is_not_related() -> None:
    got = _derive_extra(
        _e("a", 0, sectors={"finance"}, countries={"JP"}),
        _e("b", 30, sectors={"finance"}, countries={"JP"}),
    )

    assert got == {}


def test_frequent_sector_country_combo_is_not_same_target() -> None:
    """(業種, 国) の組み合わせが窓内で頻出なら「珍しい」とみなさず線にしない。"""
    extra = [_e(f"x{i}", i, sectors={"finance"}, countries={"JP"}) for i in range(10)]
    a = _e("a", 0, sectors={"finance"}, countries={"JP"})
    b = _e("b", 5, sectors={"finance"}, countries={"JP"})

    got = _derive_extra(a, b, *extra)

    assert ("a", "b") not in got
