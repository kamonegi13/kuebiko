"""配信判定の単一入口 ``decide_channel`` の回帰テスト (2026-10-11)。

v13 (M3B_RULES) 切替後 (10-10 20:33〜10-11 17:25) に alert が 0 件になった不具合は、
本番 (``_build_briefing``) の route() 直前に ``_enrich_signals_v2`` を通していたのに、
検証用の再現スクリプト (scripts/m3b_routing_replay.py) は signals を自前で組み立てていて
欠落を再現できなかったことに起因する (data/eval/replay_fix_report.md)。

再発を防ぐため:
1. ``_build_briefing`` が ``decide_channel`` 経由で配信先を決めること (二重実装にしない)
2. ``decide_channel`` (= extract_signals_from_briefing → _enrich_signals_v2 → route) が
   v13 の規則どおりに動くこと (S3∧関連性→alert、日本 targeted→japan_watch)
3. 再現スクリプトが ``route``/``evaluate_routing_rules`` を直接呼んでいないこと
   (import/トークン検査で固定)
"""

from __future__ import annotations

import io
import sys
import tokenize
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.cti.routing_decision import RoutingDecision
from src.cti.routing_v2 import M3B_RULES
from src.pipeline import briefing
from src.pipeline.summary import SummaryOutput
from src.tools.article_model import Article
from src.tools.discord_publisher import BriefingMessage

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "m3b_routing_replay.py"
_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"


def _article() -> Article:
    return Article(
        id="rss:abc",
        title="t",
        url="https://example.com/a",
        summary_html="",
        published=datetime(2026, 10, 10, 9, 0, tzinfo=UTC),
        feed_title="f",
        feed_url="https://example.com/",
    )


def _summary(**kw: object) -> SummaryOutput:
    base: dict[str, object] = {
        "title_ja": "見出し",
        "importance": "high",
        "category": "breach",
        "summary": "正常な要約です。",
    }
    return SummaryOutput(**{**base, **kw})  # type: ignore[arg-type]


def _msg() -> BriefingMessage:
    return BriefingMessage(title="t", bluf="", importance="high", category="breach", summary="s")


def _v2(**kw: Any) -> SimpleNamespace:
    base = {"severity": "S3", "severity_basis": "exploited", "jp": "none", "relevant": True}
    return SimpleNamespace(**{**base, **kw})


class TestBuildBriefingUsesDecideChannel:
    """``_build_briefing`` が configuration/route を自前に組み立てず decide_channel を呼ぶこと。"""

    def test_calls_decide_channel_once_and_uses_its_decision(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict[str, object]] = []
        decision = RoutingDecision(channel="alert", rule_id="V2.alert", reason="test-reason")

        def _stub(
            msg: BriefingMessage,
            *,
            body_text: str,
            feed_title: str,
            severity_axes: object,
            article_id: str,
            brief_count_24h: int = 0,
        ) -> RoutingDecision:
            calls.append(
                {
                    "body_text": body_text,
                    "feed_title": feed_title,
                    "article_id": article_id,
                    "brief_count_24h": brief_count_24h,
                }
            )
            return decision

        monkeypatch.setattr(briefing, "decide_channel", _stub)

        msg = briefing._build_briefing(_article(), _summary(), body_text="body", brief_count_24h=3)

        assert len(calls) == 1, "decide_channel は 1 記事 1 回だけ呼ばれるはず"
        assert calls[0]["article_id"] == "rss:abc"
        assert calls[0]["feed_title"] == "f"
        assert calls[0]["brief_count_24h"] == 3
        assert msg.metadata["target_channel"] == "alert"
        assert msg.metadata["routing_rule_id"] == "V2.alert"
        assert msg.metadata["routing_reason"] == "test-reason"


class TestDecideChannelV13Rules:
    """decide_channel (= extract_signals_from_briefing → _enrich_signals_v2 → route) が
    v13 (M3B_RULES) の規則どおりに動くこと。route() を経由する — _enrich_signals_v2 を
    直接呼ぶだけのテストでは「route() の前に enrich が渡るか」を検査できない。
    """

    @pytest.fixture(autouse=True)
    def _use_m3b_rules_engine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.cti.routing_rules as rules_mod

        monkeypatch.setenv("ROUTING_RULES_ENGINE", "1")
        monkeypatch.setattr(rules_mod, "load_routing_rules", lambda *a, **kw: M3B_RULES)

    async def test_severity_s3_and_relevant_routes_to_alert(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.cti.importance_v2_inline as inline_mod

        monkeypatch.setattr(inline_mod, "build_importance_v2_inline", lambda **_: _v2())

        decision = briefing.decide_channel(
            _msg(), body_text="b", feed_title="f", severity_axes=None, article_id="a"
        )

        assert decision.channel == "alert"

    async def test_japan_targeted_routes_to_japan_watch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.cti.importance_v2_inline as inline_mod

        monkeypatch.setattr(
            inline_mod,
            "build_importance_v2_inline",
            lambda **_: _v2(severity="S1", jp="targeted", relevant=False),
        )

        decision = briefing.decide_channel(
            _msg(), body_text="b", feed_title="f", severity_axes=None, article_id="a"
        )

        assert decision.channel == "japan_watch"

    async def test_without_enrich_everything_falls_to_watch(self) -> None:
        """enrich が効かない (severity/jp/relevant が空のまま) 場合は v13 の alert/japan_watch
        が一切発火しないことを確認する回帰テスト — これが 10-10/10-11 の不具合そのもの。
        """
        decision = briefing.decide_channel(
            _msg(), body_text="b", feed_title="f", severity_axes=None, article_id="a"
        )

        assert decision.channel == "watch"


class TestReplayScriptDoesNotDuplicateRouting:
    """再現スクリプトが route/evaluate_routing_rules を直接呼んでいないことを固定する。"""

    def test_no_direct_routing_calls_in_source(self) -> None:
        source = _SCRIPT_PATH.read_text(encoding="utf-8")
        name_tokens = {
            tok.string
            for tok in tokenize.generate_tokens(io.StringIO(source).readline)
            if tok.type == tokenize.NAME
        }
        # 文書 (docstring) 中の言及は STRING トークンになるため、NAME トークンのみを見る。
        assert "evaluate_routing_rules" not in name_tokens
        assert "route" not in name_tokens
        assert "decide_channel" in name_tokens

    def test_script_calls_decide_channel_for_routing_decision(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sys.path.insert(0, str(_SCRIPTS_DIR))
        try:
            import m3b_routing_replay as replay
        finally:
            sys.path.remove(str(_SCRIPTS_DIR))

        calls: list[str] = []
        decision = RoutingDecision(channel="alert", rule_id="V2.alert", reason="stub")

        def _stub(
            msg: object, *, body_text: str, feed_title: str, severity_axes: object, article_id: str
        ) -> RoutingDecision:
            calls.append(article_id)
            return decision

        monkeypatch.setattr(briefing, "decide_channel", _stub)

        row = replay.RoutingTimeRow(
            article_id="a1",
            title="t",
            summary="s",
            body="b",
            feed_title="f",
            category="breach",
            article_type="breaking",
            importance="high",
            old_channel="brief",
            created_at="2026-10-05T00:00:00+00:00",
            victim_sector="",
            victim_country="",
            socio_political_intent="",
            is_ransomware=False,
        )

        report = replay.run_routing_time_replay(
            [row], axes_mode="stored", days=7, samples=5, i58_cache=None
        )

        assert calls == ["a1"], "run_routing_time_replay は briefing.decide_channel を呼ぶはず"
        assert report.crosstab == {"brief -> alert": 1}
        assert report.per_day["2026-10-05"] == {"alert": 1}
