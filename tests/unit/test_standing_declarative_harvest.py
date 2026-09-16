"""段B-3c: 宣言条件による証拠収穫 — 昇格した問いに証拠が届くこと。

段B-3 までで問いは**開設できる**が、収穫は現行 4 seed の R1-R3 に固定されていたため、
昇格した問いは「器はあるが証拠が来ない」状態だった。ここでその seam を埋める。

不変条件:
1. 現行 4 seed の収穫は**一切変わらない** (R1-R3 のまま)
2. 宣言条件の問いは、条件に一致した記事だけを拾う
3. 候補プールは宣言条件の問いがあるときだけ広げる (無ければ現行の狭い SQL のまま)
"""

from __future__ import annotations

from typing import Any

from src.assessment.standing import signals_from_candidate
from src.cti.router import get_source_quality
from src.cti.routing_rules import _eval_condition

_SQ = get_source_quality()


def _cand(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "article_id": "a1",
        "intent": "espionage",
        "victim_country": "JP",
        "victim_sector": "energy",
        "importance": "high",
        "category": "apt",
        "article_type": "breaking",
        "stance": "factual_report",
    }
    base.update(over)
    return base


class TestSignalsFromCandidate:
    """候補行 + entity キー → RoutingSignals。条件評価の入力を作る。"""

    def test_maps_article_columns(self) -> None:
        s = signals_from_candidate(_cand(), entity_keys=frozenset(), nation_by_key={})

        assert s.intent == "espionage"
        assert s.victim_country == "JP"
        assert s.victim_sector == "energy"
        assert s.importance == "high"
        assert s.category == "apt"

    def test_actor_nation_comes_from_dictionary_gated_attribution(self) -> None:
        """帰属は辞書ゲート経由のみ (生のアクター名を国に直訳しない)。"""
        s = signals_from_candidate(
            _cand(),
            entity_keys=frozenset({"actor:volt typhoon"}),
            nation_by_key={"volt typhoon": "cn"},
        )

        assert s.threat_actor_nations == frozenset({"cn"})

    def test_unknown_actor_yields_no_nation(self) -> None:
        s = signals_from_candidate(
            _cand(), entity_keys=frozenset({"actor:unknown crew"}), nation_by_key={}
        )

        assert s.threat_actor_nations == frozenset()

    def test_involved_country_is_separate_from_attribution(self) -> None:
        """関与国は「記事に出ている」だけ — 帰属と混ぜない。"""
        s = signals_from_candidate(
            _cand(), entity_keys=frozenset({"involved_country:CN"}), nation_by_key={}
        )

        assert s.involved_countries == frozenset({"CN"})
        assert s.threat_actor_nations == frozenset()

    def test_missing_columns_become_empty_not_none(self) -> None:
        s = signals_from_candidate({"article_id": "a1"}, entity_keys=frozenset(), nation_by_key={})

        assert s.intent == ""
        assert s.victim_country == ""
        assert s.involved_countries == frozenset()


class TestDeclarativeConditionsEvaluate:
    """R1-R3 相当の条件が、候補行から作った signals で意図どおり効くこと。"""

    def test_r1_equivalent_matches_via_involved_country(self) -> None:
        cond = {
            "all": [
                {"property": "intent", "op": "eq", "value": "prepositioning"},
                {
                    "any": [
                        {"property": "actor_nation", "op": "in", "value": ["cn"]},
                        {"property": "involved_country", "op": "in", "value": ["CN"]},
                    ]
                },
            ]
        }
        s = signals_from_candidate(
            _cand(intent="prepositioning"),
            entity_keys=frozenset({"involved_country:CN"}),
            nation_by_key={},
        )

        assert _eval_condition(cond, s, _SQ) is True

    def test_trend_style_condition_selects_by_intent_and_sector(self) -> None:
        """型 E の証拠条件 — 現行 SQL の前絞りでは拾えない intent でも効くこと。"""
        cond = {
            "all": [
                {"property": "intent", "op": "eq", "value": "financial"},
                {"property": "victim_country", "op": "eq", "value": "JP"},
            ]
        }
        s = signals_from_candidate(
            _cand(intent="financial"), entity_keys=frozenset(), nation_by_key={}
        )

        assert _eval_condition(cond, s, _SQ) is True

    def test_condition_rejects_non_matching_article(self) -> None:
        cond = {"property": "intent", "op": "eq", "value": "prepositioning"}
        s = signals_from_candidate(
            _cand(intent="financial"), entity_keys=frozenset(), nation_by_key={}
        )

        assert _eval_condition(cond, s, _SQ) is False


class TestPoolLimits:
    """候補プールの上限と、切り詰めを黙らせないこと。"""

    def test_broad_pool_limit_covers_the_weekly_window(self) -> None:
        """実測 (2026-09-16) の weekly 母数 836 件が収まること。

        収まらないと、宣言条件の問いは weekly 実行で古い側を見落とす。
        """
        from src.assessment.standing import _BROAD_POOL_LIMIT

        assert _BROAD_POOL_LIMIT >= 836

    def test_broad_limit_is_larger_than_the_narrow_one(self) -> None:
        """粗 filter を外すと母数が跳ねるので、同じ上限では狭くなる (実測で逆転した)。"""
        from src.assessment.standing import _BROAD_POOL_LIMIT, _CANDIDATE_POOL_LIMIT

        assert _BROAD_POOL_LIMIT > _CANDIDATE_POOL_LIMIT
