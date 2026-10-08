"""src/graph/crossdomain.py — サイバーと地政学をつなぐ線の候補 (純粋関数)。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.graph.crossdomain import (
    CrossEvent,
    count_by_kind,
    derive_crossdomain,
    permuted_baseline,
)

T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _geo(i: str, days: int, involved: set[str], headline: str = "") -> CrossEvent:
    return CrossEvent(
        i, T0 + timedelta(days=days), "geo", involved=frozenset(involved), headline=headline
    )


def _cyber(
    i: str,
    days: int,
    nations: set[str],
    victims: set[str] | None = None,
    intents: set[str] | None = None,
) -> CrossEvent:
    return CrossEvent(
        i,
        T0 + timedelta(days=days),
        "cyber",
        actor_nations=frozenset(nations),
        victim_countries=frozenset(victims or ()),
        intents=frozenset(intents or ()),
    )


class TestTrigger:
    def test_geo_event_before_cyber_activity_of_same_nation(self) -> None:
        lines = derive_crossdomain([_geo("G", 0, {"CN", "TW"}), _cyber("C", 5, {"CN"})])

        assert [(x.kind, x.gap_days, x.nations) for x in lines] == [("trigger", 5, ("CN",))]

    def test_cyber_first_is_not_a_trigger(self) -> None:
        lines = derive_crossdomain([_geo("G", 5, {"CN"}), _cyber("C", 0, {"CN"})])

        assert count_by_kind(lines)["trigger"] == 0

    def test_outside_window_is_dropped(self) -> None:
        lines = derive_crossdomain([_geo("G", 0, {"CN"}), _cyber("C", 15, {"CN"})])

        assert lines == []

    def test_other_nation_does_not_link(self) -> None:
        assert derive_crossdomain([_geo("G", 0, {"RU"}), _cyber("C", 2, {"CN"})]) == []


class TestDyad:
    def test_geo_event_naming_both_actor_nation_and_victim_country(self) -> None:
        lines = derive_crossdomain(
            [_geo("G", 0, {"CN", "TW"}), _cyber("C", 3, {"CN"}, victims={"TW"})]
        )

        assert count_by_kind(lines)["dyad"] == 1
        dyad = next(x for x in lines if x.kind == "dyad")
        assert dyad.nations == ("CN", "TW")

    def test_victim_equal_to_actor_nation_is_not_a_dyad(self) -> None:
        lines = derive_crossdomain([_geo("G", 0, {"CN"}), _cyber("C", 1, {"CN"}, victims={"CN"})])

        assert count_by_kind(lines)["dyad"] == 0


class TestResponse:
    def test_attribution_report_after_cyber_activity(self) -> None:
        lines = derive_crossdomain(
            [_geo("G", 10, {"RU"}, headline="米政府がロシアを名指しで非難"), _cyber("C", 0, {"RU"})]
        )

        assert [(x.kind, x.gap_days) for x in lines] == [("response", -10)]

    def test_geo_event_without_attribution_wording_is_not_a_response(self) -> None:
        lines = derive_crossdomain(
            [_geo("G", 10, {"RU"}, headline="首脳会談が開かれた"), _cyber("C", 0, {"RU"})]
        )

        assert count_by_kind(lines)["response"] == 0


class TestIntent:
    def test_socio_political_intent_within_window(self) -> None:
        lines = derive_crossdomain(
            [_geo("G", 0, {"IR"}), _cyber("C", -4, {"IR"}, intents={"coercion"})]
        )

        assert count_by_kind(lines)["intent"] == 1

    def test_financial_intent_is_ignored(self) -> None:
        lines = derive_crossdomain(
            [_geo("G", 0, {"KP"}), _cyber("C", 2, {"KP"}, intents={"financial"})]
        )

        assert count_by_kind(lines)["intent"] == 0


class TestPermutedBaseline:
    def test_when_geo_nations_are_unique_per_event_baseline_destroys_the_links(self) -> None:
        events = [
            _geo("G1", 0, {"CN"}),
            _geo("G2", -100, {"RU"}),
            _geo("G3", -100, {"IR"}),
            _cyber("C1", 2, {"CN"}),
        ]
        real = count_by_kind(derive_crossdomain(events))["trigger"]

        base = permuted_baseline(events, rounds=60, seed=1)["trigger"]

        assert real == 1
        assert base < real

    def test_baseline_is_deterministic_for_a_seed(self) -> None:
        events = [_geo("G1", 0, {"CN"}), _geo("G2", 0, {"RU"}), _cyber("C1", 2, {"CN"})]

        assert permuted_baseline(events, seed=3) == permuted_baseline(events, seed=3)
