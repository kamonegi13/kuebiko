"""案 B / Stage 1: config 駆動 routing rule engine のテスト。

核心は **等価性**: seed (config/delivery/routing_rules.yaml) を使った engine が、現行ハードコード
ladder (_route_legacy) と同じ決定 (channel + rule_id) を返すこと (briefing 経路)。
"""

from __future__ import annotations

import itertools

import pytest

from src.cti.router import _route_legacy, get_source_quality
from src.cti.routing_rules import (
    evaluate_routing_rules,
    load_seed_from_yaml,
)
from src.cti.routing_signals import RoutingSignals


def _sig(**kwargs: object) -> RoutingSignals:
    base: dict[str, object] = {
        "source": "briefing",
        "importance": "medium",
        "article_type": "breaking",
    }
    base.update(kwargs)
    return RoutingSignals(**base)  # type: ignore[arg-type]


class TestRulesLoad:
    def test_seed_loads_with_expected_rule_ids(self) -> None:
        rules = load_seed_from_yaml()
        ids = [r["id"] for r in rules]
        # 現行 ladder の rule_id がすべて seed に存在
        for rid in [
            "R8.alert_apt_leak",
            "R2.inoreader.alert_japan_critical_apt",
            "R2.inoreader.alert_breaking_kev",
            "R3.inoreader.japan_watch",
            "R3.5.high_threat_brief",
            "R4.watch_article_type_demote",
            "R5.watch_low_confidence",
            "R5b.propaganda_demote",
            "R6.cap_demote",
            "R6.brief_importance",
            "R7.fallback",
        ]:
            assert rid in ids
        # R7 が末尾 (fallback)
        assert ids[-1] == "R7.fallback"


class TestEngineEquivalence:
    """engine == legacy (briefing 経路) を signal マトリクスで網羅検証。"""

    def test_equivalence_over_matrix(self) -> None:
        sq = get_source_quality()
        seed = load_seed_from_yaml()  # DB でなく yaml seed と legacy の等価性を検証
        snap: dict[str, object] = {}
        importances = ["high", "medium", "low"]
        article_types = ["breaking", "recap", "research", "opinion"]
        categories = ["apt", "vulnerability", "other"]  # apt/vuln=htbc, other=非htbc
        stances = ["factual_report", "propaganda"]
        bools = [False, True]

        mismatches: list[str] = []
        count = 0
        for (
            imp,
            atype,
            cat,
            stance,
            jc,
            ka,
            kev,
            zd,
            jsr,
            leak,
            secrel,
            callow,
        ) in itertools.product(
            importances,
            article_types,
            categories,
            stances,
            bools,
            bools,
            bools,
            bools,
            bools,
            bools,
            bools,
            bools,
        ):
            s = _sig(
                importance=imp,
                article_type=atype,
                category=cat,
                editorial_stance=stance,
                mentions_japan_critical=jc,
                has_known_apt=ka,
                has_kev_or_active_exploit=kev,
                is_zero_day=zd,
                is_japan_security_relevant=jsr,
                is_apt_leak=leak,
                is_security_relevant=secrel,
                confidence_all_low=callow,
            )
            legacy = _route_legacy(s, sq, snap)
            engine = evaluate_routing_rules(s, sq, snap, rules=seed)
            count += 1
            assert engine is not None, "engine は R7.fallback で必ず決定を返す"
            if (engine.channel, engine.rule_id) != (legacy.channel, legacy.rule_id):
                mismatches.append(
                    f"imp={imp} type={atype} cat={cat} stance={stance} "
                    f"jc={jc} ka={ka} kev={kev} zd={zd} jsr={jsr} leak={leak} "
                    f"secrel={secrel} callow={callow}: "
                    f"engine={engine.channel}/{engine.rule_id} != "
                    f"legacy={legacy.channel}/{legacy.rule_id}"
                )
        detail = "\n".join(mismatches[:15])
        assert not mismatches, f"{len(mismatches)}/{count} mismatch:\n{detail}"

    def test_equivalence_brief_cap(self) -> None:
        """R6.cap_demote (量制御) の境界も一致すること。"""
        sq = get_source_quality()
        seed = load_seed_from_yaml()
        snap: dict[str, object] = {}
        for imp in ["high", "medium"]:
            for cnt in [0, sq.brief_cap_24h, sq.brief_cap_24h + 5]:
                s = _sig(
                    importance=imp,
                    category="other",  # htbc 外 → R3.5 を avoid、R6 経路に乗せる
                    article_type="breaking",
                    is_security_relevant=True,
                    brief_count_24h_snapshot=cnt,
                )
                legacy = _route_legacy(s, sq, snap)
                engine = evaluate_routing_rules(s, sq, snap, rules=seed)
                assert engine is not None
                assert (engine.channel, engine.rule_id) == (legacy.channel, legacy.rule_id), (
                    f"imp={imp} cnt={cnt}: engine={engine.rule_id} legacy={legacy.rule_id}"
                )


class TestRuleValidation:
    """Stage 2: 保存前検証 (silent typo 防止)。"""

    def test_seed_is_valid(self) -> None:
        from src.config_loader import KNOWN_ARTICLE_CATEGORIES
        from src.cti.routing_rules import validate_routing_rules

        errs = validate_routing_rules(load_seed_from_yaml(), set(KNOWN_ARTICLE_CATEGORIES))
        assert errs == []

    def test_unknown_channel_and_property_detected(self) -> None:
        # 語彙統一: flag は boolean プロパティに吸収。未知 flag = 未知プロパティ。
        from src.cti.routing_rules import validate_routing_rules

        bad = [{"id": "x", "channel": "nope", "when": {"flag": "bogus"}}]
        errs = validate_routing_rules(bad, set())
        assert any("channel" in e for e in errs)
        assert any("bogus" in e for e in errs)  # 未知プロパティとして検出

    def test_unknown_predicate_detected(self) -> None:
        from src.cti.routing_rules import validate_routing_rules

        bad = [{"id": "x", "channel": "watch", "when": {"all": [{"frobnicate": True}]}}]
        errs = validate_routing_rules(bad, set())
        assert any("未知のプロパティ" in e for e in errs)

    def test_missing_channel_detected(self) -> None:
        from src.cti.routing_rules import validate_routing_rules

        errs = validate_routing_rules([{"id": "x", "when": {"always": True}}], set())
        assert any("channel" in e for e in errs)

    def test_empty_rules_invalid(self) -> None:
        from src.cti.routing_rules import validate_routing_rules

        assert validate_routing_rules([], set())


class TestHarvestedConditions:
    """語彙拡張 ① (回収): max_cvss(数値) / actor_nation(集合) / victim_sector(文字列)。"""

    def _sq(self) -> object:
        return get_source_quality()

    def test_max_cvss_numeric(self) -> None:
        from src.cti.routing_rules import _eval_condition

        sq = self._sq()
        sig = _sig(max_cvss=9.5)
        assert _eval_condition({"max_cvss": {"gte": 9.0}}, sig, sq) is True  # type: ignore[arg-type]
        assert _eval_condition({"max_cvss": {"gte": 9.6}}, sig, sq) is False  # type: ignore[arg-type]
        assert _eval_condition({"max_cvss": {"lt": 9.6}}, sig, sq) is True  # type: ignore[arg-type]

    def test_actor_nation_set(self) -> None:
        from src.cti.routing_rules import _eval_condition

        sq = self._sq()
        sig = _sig(threat_actor_nations=frozenset({"cn", "ru"}))
        assert _eval_condition({"actor_nation": {"in": ["cn"]}}, sig, sq) is True  # type: ignore[arg-type]
        assert _eval_condition({"actor_nation": {"in": ["kp"]}}, sig, sq) is False  # type: ignore[arg-type]
        assert _eval_condition({"actor_nation": {"not_in": ["kp"]}}, sig, sq) is True  # type: ignore[arg-type]
        # アクター nation 未検出 → in は必ず False
        assert _eval_condition({"actor_nation": {"in": ["cn"]}}, _sig(), sq) is False  # type: ignore[arg-type]

    def test_victim_sector_str(self) -> None:
        from src.cti.routing_rules import _eval_condition

        sq = self._sq()
        sig = _sig(victim_sector="healthcare")
        assert _eval_condition({"victim_sector": {"eq": "healthcare"}}, sig, sq) is True  # type: ignore[arg-type]
        cond = {"victim_sector": {"in": ["finance", "healthcare"]}}
        assert _eval_condition(cond, sig, sq) is True  # type: ignore[arg-type]
        assert _eval_condition({"victim_sector": {"eq": "energy"}}, sig, sq) is False  # type: ignore[arg-type]

    def test_keyword_list_set(self) -> None:
        # 語彙拡張②: user 定義 match_list の判定 (set 条件)。
        from src.cti.routing_rules import _eval_condition

        sq = self._sq()
        sig = _sig(matched_keyword_lists=frozenset({"vendors"}))
        assert _eval_condition({"keyword_list": {"in": ["vendors"]}}, sig, sq) is True  # type: ignore[arg-type]
        assert _eval_condition({"keyword_list": {"in": ["cloud"]}}, sig, sq) is False  # type: ignore[arg-type]
        assert _eval_condition({"keyword_list": {"not_in": ["cloud"]}}, sig, sq) is True  # type: ignore[arg-type]

    def test_validation_accepts_new_fields(self) -> None:
        from src.cti.routing_rules import valid_channels, validate_routing_rules

        ch = next(iter(valid_channels()))
        good = [
            {"id": "a", "channel": ch, "when": {"max_cvss": {"gte": 9.0}}},
            {"id": "b", "channel": ch, "when": {"actor_nation": {"in": ["cn", "ru"]}}},
            {"id": "c", "channel": ch, "when": {"victim_sector": {"eq": "healthcare"}}},
        ]
        assert validate_routing_rules(good, set()) == []

    def test_validation_rejects_bad_ops(self) -> None:
        from src.cti.routing_rules import valid_channels, validate_routing_rules

        ch = next(iter(valid_channels()))
        bad = [
            {"id": "a", "channel": ch, "when": {"max_cvss": {"in": [9]}}},  # 数値に in 不可
            {"id": "b", "channel": ch, "when": {"actor_nation": {"gte": 1}}},  # 集合に gte 不可
        ]
        errs = validate_routing_rules(bad, set())
        assert any("max_cvss" in e for e in errs)
        assert any("actor_nation" in e for e in errs)


class TestPreviewDiff:
    """Stage 2: 代表シナリオの 旧 vs 新 差分検出。"""

    def test_seed_shows_no_change_vs_legacy(self) -> None:
        from src.cti.router import _route_legacy, get_source_quality
        from src.cti.routing_rules import (
            PREVIEW_SCENARIOS,
            evaluate_routing_rules,
            load_seed_from_yaml,
        )
        from src.cti.routing_signals import RoutingSignals

        sq = get_source_quality()
        seed = load_seed_from_yaml()
        for _name, kw in PREVIEW_SCENARIOS:
            base = {"source": "briefing", "importance": "medium", "article_type": "breaking"}
            base.update(kw)
            s = RoutingSignals(**base)  # type: ignore[arg-type]
            cur = _route_legacy(s, sq, {})
            prop = evaluate_routing_rules(s, sq, {}, rules=seed)
            assert prop is not None and prop.channel == cur.channel

    def test_modified_rule_changes_recap_scenario(self) -> None:
        from src.cti.router import get_source_quality
        from src.cti.routing_rules import evaluate_routing_rules, load_seed_from_yaml
        from src.cti.routing_signals import RoutingSignals

        # R4 (article_type recap → watch) を brief に書き換えた提案
        proposed = [dict(r) for r in load_seed_from_yaml()]
        for r in proposed:
            if r.get("id") == "R4.watch_article_type_demote":
                r["channel"] = "brief"
        sq = get_source_quality()
        s = RoutingSignals(
            source="briefing", importance="high", category="apt", article_type="recap"
        )
        prop = evaluate_routing_rules(s, sq, {}, rules=proposed)
        assert prop is not None and prop.channel == "brief"  # 旧は watch → 変化


class TestLoadRoutingRulesDbFirst:
    """DB を正とし、未保存時のみ yaml seed に fallback する挙動。"""

    def test_db_value_preferred(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.storage.config_store as cstore
        from src.cti.routing_rules import invalidate_rules_cache, load_routing_rules

        invalidate_rules_cache()
        custom = [{"id": "X", "channel": "alert", "when": {"always": True}}]
        monkeypatch.setattr(cstore, "get_config", lambda key, **kw: custom)
        assert load_routing_rules(force_reload=True) == custom
        invalidate_rules_cache()

    def test_yaml_fallback_when_db_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.storage.config_store as cstore
        from src.cti.routing_rules import invalidate_rules_cache, load_routing_rules

        invalidate_rules_cache()
        monkeypatch.setattr(cstore, "get_config", lambda key, **kw: None)
        ids = [r["id"] for r in load_routing_rules(force_reload=True)]
        assert "R7.fallback" in ids  # yaml seed に degrade
        invalidate_rules_cache()


class TestExposedFlags:
    """フラグ全公開: 未公開だった既存 RoutingSignals bool も評価できる。"""

    def test_new_flags_in_vocab(self) -> None:
        from src.cti.routing_rules import VALID_FLAGS

        for f in ["japan_mentioned", "llm_breaking_critical", "llm_japan_targeted"]:
            assert f in VALID_FLAGS

    def test_new_flag_evaluates(self) -> None:
        rules = [
            {"id": "x", "channel": "alert", "when": {"flag": "japan_mentioned"}},
            {"id": "f", "channel": "watch", "when": {"always": True}},
        ]
        s = _sig(mentions_japan=True)
        d = evaluate_routing_rules(s, get_source_quality(), {}, rules=rules)
        assert d is not None and d.channel == "alert"
        # false なら fallback
        s2 = _sig(mentions_japan=False)
        d2 = evaluate_routing_rules(s2, get_source_quality(), {}, rules=rules)
        assert d2 is not None and d2.channel == "watch"
