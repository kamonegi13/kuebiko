"""v13 の配信ルールに本番の signals で severity / jp / relevant が渡る回帰テスト (2026-10-11)。

v13 (M3B_RULES) にした 10-10 20:33 以降、本番の route() に severity / jp / relevant が空のまま
渡り、alert 0・japan_watch 1 件になっていた (shadow だけが計算していた)。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from src.cti.router import get_source_quality
from src.cti.routing_rules import evaluate_routing_rules
from src.cti.routing_signals import RoutingSignals
from src.cti.routing_v2 import M3B_RULES
from src.pipeline import briefing
from src.tools.discord_publisher import BriefingMessage


def _msg() -> BriefingMessage:
    return BriefingMessage(title="t", bluf="", importance="high", category="breach", summary="s")


def _sig() -> RoutingSignals:
    return RoutingSignals(source="briefing", importance="high", article_type="breaking")


def _v2(**kw: Any) -> SimpleNamespace:
    base = {"severity": "S3", "severity_basis": "exploited", "jp": "none", "relevant": True}
    return SimpleNamespace(**{**base, **kw})


def test_enrich_fills_v2_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.cti.importance_v2_inline as mod

    monkeypatch.setattr(mod, "build_importance_v2_inline", lambda **_: _v2())
    out = briefing._enrich_signals_v2(
        _sig(),
        msg=_msg(),
        body_text="b",
        feed_title="f",
        severity_axes=None,
        article_id="a",
    )
    assert (out.severity, out.relevant, out.jp) == ("S3", True, "none")


def test_enrich_failure_keeps_original(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.cti.importance_v2_inline as mod

    def _boom(**_: object) -> object:
        raise RuntimeError("x")

    monkeypatch.setattr(mod, "build_importance_v2_inline", _boom)
    original = _sig()
    out = briefing._enrich_signals_v2(
        original, msg=_msg(), body_text="b", feed_title="f", severity_axes=None, article_id="a"
    )
    assert out is original


def test_v13_routes_enriched_s3_relevant_to_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.cti.importance_v2_inline as mod

    monkeypatch.setattr(mod, "build_importance_v2_inline", lambda **_: _v2())
    signals = briefing._enrich_signals_v2(
        _sig(),
        msg=_msg(),
        body_text="b",
        feed_title="f",
        severity_axes=None,
        article_id="a",
    )
    decision = evaluate_routing_rules(signals, get_source_quality(), {}, rules=M3B_RULES)
    assert decision is not None and decision.channel == "alert"


def test_v13_routes_japan_targeted_to_japan_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.cti.importance_v2_inline as mod

    monkeypatch.setattr(
        mod,
        "build_importance_v2_inline",
        lambda **_: _v2(severity="S1", jp="targeted", relevant=False),
    )
    signals = briefing._enrich_signals_v2(
        _sig(),
        msg=_msg(),
        body_text="b",
        feed_title="f",
        severity_axes=None,
        article_id="a",
    )
    decision = evaluate_routing_rules(signals, get_source_quality(), {}, rules=M3B_RULES)
    assert decision is not None and decision.channel == "japan_watch"
