"""src.cti.article_classifier のテスト (Phase 5K)。"""

from __future__ import annotations

from src.cti.article_classifier import detect_article_type


class TestDetectArticleType:
    def test_weekly_recap_detected(self) -> None:
        assert detect_article_type("Weekly Recap: AI-Powered Phishing") == "recap"

    def test_monthly_roundup_detected(self) -> None:
        assert detect_article_type("Monthly Roundup of CVEs") == "recap"

    def test_top_n_detected(self) -> None:
        assert detect_article_type("Top 10 Ways Hackers Use AI") == "recap"
        assert detect_article_type("5 Best Pentest Tools 2026") == "recap"

    def test_tutorial_detected(self) -> None:
        assert detect_article_type("How to Detect APT41") == "tutorial"
        assert detect_article_type("A Guide to MITRE ATT&CK") == "tutorial"

    def test_press_release_detected(self) -> None:
        assert detect_article_type("Cisco Launches AI Provenance Tool") == "press"
        assert detect_article_type("Microsoft Announces New Defender Feature") == "press"

    def test_japanese_recap_detected(self) -> None:
        assert detect_article_type("週間サイバー攻撃まとめ") == "recap"
        assert detect_article_type("今月の脆弱性振り返り") == "recap"
        assert detect_article_type("月間ランサムウェア総括") == "recap"

    def test_japanese_tutorial_detected(self) -> None:
        assert detect_article_type("APT 入門") == "tutorial"
        assert detect_article_type("ゼロデイ徹底解説") == "tutorial"

    def test_normal_breaking_news_falls_through(self) -> None:
        # 普通の breaking タイトルはそのまま
        assert detect_article_type("Microsoft Patches Critical RCE in Exchange") == "breaking"

    def test_llm_hint_used_when_no_regex_match(self) -> None:
        assert detect_article_type("Generic Title", llm_hint="advisory") == "advisory"
        assert detect_article_type("Generic Title", llm_hint="opinion") == "opinion"

    def test_invalid_llm_hint_falls_back(self) -> None:
        assert detect_article_type("Generic Title", llm_hint="invalid") == "breaking"

    def test_regex_overrides_llm_hint(self) -> None:
        """LLM が breaking と判定しても title regex が recap を見つけたら recap が勝つ。"""
        assert detect_article_type("Weekly Recap of attacks", llm_hint="breaking") == "recap"

    def test_empty_title_falls_back_to_hint(self) -> None:
        assert detect_article_type("", llm_hint="advisory") == "advisory"
        assert detect_article_type("", llm_hint=None) == "breaking"
