"""段B-1: 問いの証拠条件を宣言文法で書けるようにする property 追加。

**なぜ要るか** (docs/pir_brief_design.md §6c): 常設情報要求 (真の PIR) の証拠適格は
いま `src/assessment/standing.py` に R1-R3 として手書きされている。型を 1 つ増やすたびに
Python を書く必要があり、これが「型の増設が高い」の実体だった。

`intent` / `victim_country` / `involved_country` を `PROPERTY_CATALOG` に載せると
R1-R3 が routing/PIR と共有の match ツリー (all/any/not + 葉) で書けるようになり、
証拠条件が**データ (UI 編集可)** になる。型の増設コストは仮説 3-4 件の記述だけに落ちる。

**等価性がこのファイルの主題** — 宣言版が手書き R1-R3 と同じ判定を返すことを固定する。
これが崩れると、問いの証拠が静かに変わる (台帳は「何を証拠に採ったか」を後から追えない)。
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from src.cti.router import get_source_quality
from src.cti.routing_rules import PROPERTY_BY_ID, _eval_condition
from src.cti.routing_signals import RoutingSignals

# --- R1-R3 の宣言版 (docs/pir_brief_design.md §6c、手書きは standing.py:_match_rule) ---
#
# R1 直接:   intent=prepositioning かつ (アクター国籍 N または 関与国 N)
# R2 隣接:   intent∈(espionage,disruption) かつ アクター国籍 N かつ CI セクタ
# R3 JP観測: victim_country=JP かつ CI セクタ かつ アクター国籍 N
#
# ⚠ R1 だけ「関与国」を含む非対称は**意図的**: R2/R3 は辞書ゲート済み帰属のみを使う
# (過剰帰属の再発防止、prepositioning_posture_ledger_design.md §3.1)。宣言版でも保つ。

_CI_SECTORS = ("energy", "telecom", "finance", "government", "transport")


def _leaf(prop: str, op: str, value: Any) -> dict[str, Any]:
    return {"property": prop, "op": op, "value": value}


def r1_tree(nation: str) -> dict[str, Any]:
    return {
        "all": [
            _leaf("intent", "eq", "prepositioning"),
            {
                "any": [
                    _leaf("actor_nation", "in", [nation]),
                    _leaf("involved_country", "in", [nation.upper()]),
                ]
            },
        ]
    }


def r2_tree(nation: str) -> dict[str, Any]:
    return {
        "all": [
            _leaf("intent", "in", ["espionage", "disruption"]),
            _leaf("actor_nation", "in", [nation]),
            _leaf("victim_sector", "in", list(_CI_SECTORS)),
        ]
    }


def r3_tree(nation: str) -> dict[str, Any]:
    return {
        "all": [
            _leaf("victim_country", "eq", "JP"),
            _leaf("victim_sector", "in", list(_CI_SECTORS)),
            _leaf("actor_nation", "in", [nation]),
        ]
    }


_SQ = get_source_quality()


def _sig(**kwargs: Any) -> RoutingSignals:
    base: dict[str, Any] = {
        "source": "briefing",
        "importance": "medium",
        "article_type": "breaking",
    }
    base.update(kwargs)
    return RoutingSignals(**base)


class TestPropertiesExist:
    """3 つが型付きでカタログに載ること (UI の条件エディタはカタログ駆動)。"""

    @pytest.mark.parametrize(
        ("prop_id", "kind"),
        [("intent", "str"), ("victim_country", "str"), ("involved_country", "set")],
    )
    def test_property_is_registered_with_kind(self, prop_id: str, kind: str) -> None:
        spec = PROPERTY_BY_ID.get(prop_id)

        assert spec is not None, f"{prop_id} が PROPERTY_CATALOG に無い"
        assert spec.kind == kind

    @pytest.mark.parametrize(
        ("prop_id", "domain"),
        [
            ("intent", "intents"),
            ("victim_country", "countries"),
            ("involved_country", "countries"),
        ],
    )
    def test_property_declares_vocab_domain(self, prop_id: str, domain: str) -> None:
        """値域は SSoT 参照 (複製辞書を作らない — CLAUDE.md §7 の 3 点セット)。"""
        assert PROPERTY_BY_ID[prop_id].domain == domain


class TestAccessorsReadSignals:
    def test_intent_accessor_reads_signal(self) -> None:
        spec = PROPERTY_BY_ID["intent"]

        assert spec.accessor(_sig(intent="prepositioning"), _SQ) == "prepositioning"

    def test_victim_country_accessor_reads_signal(self) -> None:
        spec = PROPERTY_BY_ID["victim_country"]

        assert spec.accessor(_sig(victim_country="JP"), _SQ) == "JP"

    def test_involved_country_accessor_reads_signal(self) -> None:
        spec = PROPERTY_BY_ID["involved_country"]

        actual = cast(
            frozenset[str], spec.accessor(_sig(involved_countries=frozenset({"CN", "TW"})), _SQ)
        )

        assert set(actual) == {"CN", "TW"}

    def test_missing_values_are_empty_not_none(self) -> None:
        """未判定を None にしない — 葉の比較で TypeError を出さないため。"""
        s = _sig()

        assert PROPERTY_BY_ID["intent"].accessor(s, _SQ) == ""
        assert PROPERTY_BY_ID["victim_country"].accessor(s, _SQ) == ""
        involved = cast(frozenset[str], PROPERTY_BY_ID["involved_country"].accessor(s, _SQ))

        assert set(involved) == set()


class TestR1DeclarativeEquivalence:
    """R1: prepositioning × (アクター国籍 または 関与国)。"""

    def test_matches_on_actor_nation(self) -> None:
        s = _sig(intent="prepositioning", threat_actor_nations=frozenset({"cn"}))

        assert _eval_condition(r1_tree("cn"), s, _SQ) is True

    def test_matches_on_involved_country_without_attribution(self) -> None:
        """R1 は帰属が無くても関与国で拾う (事前配置は帰属前に兆候が出る)。"""
        s = _sig(intent="prepositioning", involved_countries=frozenset({"CN"}))

        assert _eval_condition(r1_tree("cn"), s, _SQ) is True

    def test_rejects_other_intent(self) -> None:
        s = _sig(intent="financial", threat_actor_nations=frozenset({"cn"}))

        assert _eval_condition(r1_tree("cn"), s, _SQ) is False

    def test_rejects_other_nation(self) -> None:
        s = _sig(intent="prepositioning", threat_actor_nations=frozenset({"ru"}))

        assert _eval_condition(r1_tree("cn"), s, _SQ) is False


class TestR2DeclarativeEquivalence:
    """R2: 隣接動機 × 帰属済みアクター国籍 × CI セクタ。"""

    def test_matches_espionage_against_ci_sector(self) -> None:
        s = _sig(
            intent="espionage",
            threat_actor_nations=frozenset({"cn"}),
            victim_sector="energy",
        )

        assert _eval_condition(r2_tree("cn"), s, _SQ) is True

    def test_rejects_non_ci_sector(self) -> None:
        """較正 (07-13): セクタ無しの一般 malware 記事を吸わせない。"""
        s = _sig(
            intent="espionage",
            threat_actor_nations=frozenset({"cn"}),
            victim_sector="retail",
        )

        assert _eval_condition(r2_tree("cn"), s, _SQ) is False

    def test_rejects_involved_country_only(self) -> None:
        """R2 は帰属を要求する — 関与国だけでは入らない (R1 との非対称を保つ)。"""
        s = _sig(intent="espionage", involved_countries=frozenset({"CN"}), victim_sector="energy")

        assert _eval_condition(r2_tree("cn"), s, _SQ) is False


class TestR3DeclarativeEquivalence:
    """R3: JP 被害 × CI セクタ × 帰属済みアクター国籍。"""

    def test_matches_attributed_jp_ci_victim(self) -> None:
        s = _sig(
            victim_country="JP",
            victim_sector="telecom",
            threat_actor_nations=frozenset({"kp"}),
        )

        assert _eval_condition(r3_tree("kp"), s, _SQ) is True

    def test_rejects_unattributed_jp_event(self) -> None:
        """帰属なし JP 事象は standing に入れない (過剰帰属の再発防止)。"""
        s = _sig(victim_country="JP", victim_sector="telecom")

        assert _eval_condition(r3_tree("kp"), s, _SQ) is False

    def test_rejects_non_jp_victim(self) -> None:
        s = _sig(
            victim_country="KR",
            victim_sector="telecom",
            threat_actor_nations=frozenset({"kp"}),
        )

        assert _eval_condition(r3_tree("kp"), s, _SQ) is False


class TestDomainResolution:
    """値域は SSoT から解決される (UI の選択肢がここから出る)。"""

    def test_intents_domain_matches_diamond_model(self) -> None:
        from src.cti.diamond_model import SOCIO_POLITICAL_INTENTS
        from src.ui.api.routing_rules import _resolve_domain

        assert set(_resolve_domain("intents")) == set(SOCIO_POLITICAL_INTENTS)

    def test_countries_domain_is_non_empty_and_contains_jp(self) -> None:
        from src.ui.api.routing_rules import _resolve_domain

        values = _resolve_domain("countries")

        assert "JP" in values
        assert len(values) > 100
