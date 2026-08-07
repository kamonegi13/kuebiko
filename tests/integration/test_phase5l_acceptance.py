"""Phase 5L acceptance テスト (10 問題の回帰防止)。

過去 24h で観測された誤分類 / 重複 / HTML 混入 / 観測性不足の各問題が
解消されていることを統合的に検証する。

各テストは 1 問題 1 ケースで、Phase 5L-1〜5L-5 の修正がすべて噛み合って
動作することを確認する。
"""

from __future__ import annotations

from src.cti.dedup_key import compute_dedup_key
from src.cti.llm_routing_flags import parse_routing_flags
from src.cti.router import route
from src.cti.routing_signals import extract_signals_from_briefing
from src.tools.discord_publisher import BriefingMessage
from src.tools.text_sanitizer import has_html_residue, sanitize_for_display


def _msg(
    *,
    title: str = "Sample",
    bluf: str = "BLUF",
    summary: str = "summary",
    importance: str = "high",
    category: str = "vulnerability",
    metadata: dict[str, object] | None = None,
) -> BriefingMessage:
    return BriefingMessage(
        title=title,
        bluf=bluf,
        summary=summary,
        importance=importance,  # type: ignore[arg-type]
        category=category,
        metadata=metadata or {},
    )


class TestPhase5LAcceptance:
    """10 問題の代表ケースが解消されていることを検証する。"""

    def test_problem_1_japan_watch_misclassification_resolved(self) -> None:
        """問題 ①: グローバル CVE が「国内」一語で japan_watch に流れない。"""
        msg = _msg(
            title="Linux カーネル Copy Fail (CVE-2026-31431) 詳細",
            bluf="グローバルな Linux カーネルの脆弱性。",
            summary="国内外の組織が Linux カーネルの脆弱性 CVE-2026-31431 の影響を受ける可能性。",
            metadata={
                "routing_flags": {
                    "japan_targeted": False,
                    "is_breaking_critical": True,
                    "primary_actor_id": "",
                    "dedup_key": "cve-2026-31431",
                    "confidence": "high",
                },
                "article_type": "advisory",
            },
        )
        signals = extract_signals_from_briefing(msg, body_text="Linux kernel global CVE")
        decision = route(signals)
        # 旧: japan_watch → 修正後: brief (importance=high + advisory + LLM が日本標的でないと明言)
        assert decision.channel != "japan_watch"
        assert decision.channel == "brief"

    def test_problem_2_dedup_key_clusters_same_event(self) -> None:
        """問題 ②: 同一インシデントは同じ dedup_key を持つ (LLM 提供 key 活用)。"""
        msg_a = _msg(
            title="Salt Typhoon が IBM 子会社を侵害",
            metadata={
                "routing_flags": {
                    "japan_targeted": False,
                    "is_breaking_critical": True,
                    "primary_actor_id": "salt-typhoon",
                    "dedup_key": "salt-typhoon-ibm-italy",
                    "confidence": "high",
                },
            },
        )
        msg_b = _msg(
            title="サプライチェーン攻撃で Sistemi Informativi が侵入される",
            metadata={
                "routing_flags": {
                    "japan_targeted": False,
                    "is_breaking_critical": True,
                    "primary_actor_id": "salt-typhoon",
                    "dedup_key": "salt-typhoon-ibm-italy",
                    "confidence": "high",
                },
            },
        )
        # 両方とも LLM が同じ dedup_key を出していれば、ch 横断 dedup で 1 件のみ投稿される
        flags_a = parse_routing_flags(msg_a.metadata.get("routing_flags"))
        flags_b = parse_routing_flags(msg_b.metadata.get("routing_flags"))
        key_a = compute_dedup_key(
            llm_key=flags_a.dedup_key,
            title=msg_a.title,
            article_id_fallback="aid-a",
        )
        key_b = compute_dedup_key(
            llm_key=flags_b.dedup_key,
            title=msg_b.title,
            article_id_fallback="aid-b",
        )
        assert key_a == key_b == "salt-typhoon-ibm-italy"

    def test_problem_3_html_residue_stripped(self) -> None:
        """問題 ③: タイトル末尾の HTML タグ残骸が除去される。"""
        raw = "Conti および Akira ランサムウェアの提携者が 8 年</td>"
        cleaned = sanitize_for_display(raw)
        assert "<" not in cleaned
        assert "</td>" not in cleaned
        assert has_html_residue(cleaned) is False

    def test_problem_5_recap_does_not_pollute_brief(self) -> None:
        """問題 ⑤: Weekly Recap (article_type=recap) は importance high でも watch に降格。"""
        from src.cti.routing_signals import RoutingSignals

        signals = RoutingSignals(
            source="briefing",
            importance="high",
            article_type="recap",
            is_security_relevant=True,
        )
        decision = route(signals)
        assert decision.channel == "watch"
        assert decision.rule_id == "R4.watch_article_type_demote"

    def test_problem_8_routing_decision_has_rule_id_for_observability(self) -> None:
        """問題 ⑧: route() が rule_id + reason + signals_snapshot を返し grep デバッグできる。"""
        from src.cti.routing_signals import RoutingSignals

        signals = RoutingSignals(
            source="briefing",
            importance="high",
            article_type="breaking",
            is_security_relevant=True,
        )
        decision = route(signals)
        # 観測性: 全フィールド必須
        assert decision.channel
        assert decision.rule_id  # 必ず R1〜R7 のいずれか
        assert decision.reason
        assert decision.signals_snapshot
        assert "article_type" in decision.signals_snapshot

    def test_problem_japan_kana_negative_lookahead(self) -> None:
        """日本語/日本食/日本酒 等の汎用語は japan match しない (5L-1 検証)。"""
        from src.cti.routing_signals import _JAPAN_GENERAL_PATTERNS

        for false_positive in ["日本語", "日本食", "日本酒", "日本料理", "日本文化", "日本海"]:
            assert _JAPAN_GENERAL_PATTERNS.search(false_positive) is None, (
                f"{false_positive} should not match"
            )
        # 正常系: 「日本」単体や「日本企業」「日本のセキュリティ」は match
        assert _JAPAN_GENERAL_PATTERNS.search("日本企業が侵害された") is not None

    def test_problem_japanese_substring_does_not_match_japan_uppercase(self) -> None:
        """JAPANESE のような単語境界違反で誤検知しない (5L-1 検証)。"""
        from src.cti.routing_signals import _JAPAN_GENERAL_PATTERNS

        assert _JAPAN_GENERAL_PATTERNS.search("JAPANESE researchers explain") is None
        # 正常系: 単語として Japan が出る場合
        assert _JAPAN_GENERAL_PATTERNS.search("targeted Japan and Korea") is not None

    def test_problem_low_confidence_falls_back_to_regex(self) -> None:
        """LLM confidence=low のとき regex フォールバック動作 (5L-3 二重防御)。

        Phase 5O: JPCERT/NISC 等の発信者は CRITICAL regex から削除済。
        Japan-targeted の確実な signal (「自衛隊」「日本企業」等) のみ match する。
        """
        msg = _msg(
            title="Notice",
            summary="自衛隊への攻撃が観測された。",
            metadata={
                "routing_flags": {
                    "japan_targeted": False,
                    "is_breaking_critical": False,
                    "primary_actor_id": "",
                    "dedup_key": "",
                    "confidence": "low",  # LLM 不確実 → regex 採用
                },
            },
        )
        signals = extract_signals_from_briefing(msg, body_text="自衛隊への攻撃")
        # 自衛隊は Phase 5O 後も CRITICAL regex に hit (Japan-targeted 強 signal)
        assert signals.mentions_japan_critical is True
