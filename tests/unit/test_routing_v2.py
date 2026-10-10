"""M3b 段1: 重要度の再設計ベースの新ルール (``src/cti/routing_v2.M3B_RULES``) のテスト。

本番の投稿先には使わない (shadow 専用)。評価の核心だけを確認する — 4 rule + 境界値。
"""

from __future__ import annotations

import pytest

from src.cti import routing_rules
from src.cti.router import get_source_quality
from src.cti.routing_rules import evaluate_routing_rules, validate_routing_rules
from src.cti.routing_signals import RoutingSignals
from src.cti.routing_v2 import M3B_RULES


def _sig(**kwargs: object) -> RoutingSignals:
    base: dict[str, object] = {
        "source": "briefing",
        "importance": "medium",
        "article_type": "breaking",
    }
    base.update(kwargs)
    return RoutingSignals(**base)  # type: ignore[arg-type]


def _decide(signals: RoutingSignals) -> str:
    decision = evaluate_routing_rules(signals, get_source_quality(), {}, rules=M3B_RULES)
    assert decision is not None  # V2.fallback は always:true なので None にはならない
    return decision.channel


class TestM3bRulesCoreBehavior:
    def test_s3_relevant_non_derivative_goes_to_alert(self) -> None:
        sig = _sig(severity="S3", relevant=True, article_type="breaking")
        assert _decide(sig) == "alert"

    def test_s3_kev_not_relevant_goes_to_vuln(self) -> None:
        sig = _sig(severity="S3", relevant=False, severity_basis="kev")
        assert _decide(sig) == "vuln"

    def test_japan_targeted_or_affected_goes_to_japan_watch(self) -> None:
        for jp in ("targeted", "affected"):
            sig = _sig(severity="", relevant=False, jp=jp)
            assert _decide(sig) == "japan_watch"

    def test_no_match_falls_back_to_watch(self) -> None:
        sig = _sig(severity="", relevant=False, jp="none")
        assert _decide(sig) == "watch"


class TestM3bRulesEdgeCases:
    def test_s3_targeted_relevant_is_alert_even_with_jp_affected(self) -> None:
        # alert が先に評価されるので jp.affected より優先 (first-match)
        sig = _sig(severity="S3", relevant=True, jp="affected", article_type="breaking")
        assert _decide(sig) == "alert"

    def test_s3_kev_but_relevant_true_does_not_go_to_vuln(self) -> None:
        # V2.alert が先に一致する (severity=S3 + relevant=True)
        sig = _sig(severity="S3", relevant=True, severity_basis="kev", article_type="breaking")
        assert _decide(sig) == "alert"

    def test_s3_confirmed_wide_harm_not_relevant_falls_back_to_watch(self) -> None:
        # severity_basis が V2.vuln の対象 (kev/exploited/exploited_product) に無いので
        # vuln に行かず、jp も無いので fallback
        sig = _sig(severity="S3", relevant=False, severity_basis="confirmed_wide_harm", jp="none")
        assert _decide(sig) == "watch"

    def test_s3_relevant_but_press_article_type_is_not_alert(self) -> None:
        sig = _sig(severity="S3", relevant=True, article_type="press", jp="none")
        assert _decide(sig) != "alert"
        assert _decide(sig) == "watch"

    def test_empty_severity_goes_to_watch(self) -> None:
        sig = _sig(severity="", relevant=False, jp="none")
        assert _decide(sig) == "watch"

    def test_jp_targeted_with_s2_severity_goes_to_japan_watch(self) -> None:
        sig = _sig(severity="S2", relevant=True, jp="targeted", article_type="breaking")
        assert _decide(sig) == "japan_watch"


class TestM3bRulesValidation:
    def test_validates_once_vuln_channel_is_registered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``vuln`` は未登録 channel。登録済みとして振る舞わせれば検証を通る。"""
        monkeypatch.setattr(
            routing_rules,
            "valid_channels",
            lambda: frozenset({"alert", "watch", "japan_watch", "vuln"}),
        )
        errs = validate_routing_rules(M3B_RULES, set())
        assert errs == []

    def test_fails_validation_while_vuln_channel_is_unregistered(self) -> None:
        """現状 (``vuln`` 未登録) では検証が落ちることを確認 (shadow 専用である所在確認)。"""
        errs = validate_routing_rules(M3B_RULES, set())
        assert any("vuln" in e for e in errs)
