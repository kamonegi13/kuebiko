"""src.cti.router のテスト (Phase 5L-1: RoutingDecision 構造化版)。"""

from __future__ import annotations

import pytest

from src.cti.router import route
from src.cti.routing_decision import RoutingDecision
from src.cti.routing_signals import RoutingSignals


def _signals(**kwargs: object) -> RoutingSignals:
    """テスト用 RoutingSignals ファクトリ (必須引数のみ事前設定)。"""
    defaults: dict[str, object] = {
        "source": "briefing",
        "importance": "medium",
        "article_type": "breaking",
    }
    defaults.update(kwargs)
    return RoutingSignals(**defaults)  # type: ignore[arg-type]


# ---------- Inoreader 経路 ----------


class TestR35HighThreatBrief:
    """Phase B-cal R3.5: 高重要度の脅威 writeup は style 降格 (R4) より優先して brief。

    監査で 88 件の high(apt/malware/vuln)→watch 流出を確認。技術解析・考察スタイルで
    article_type=research/opinion に分類されても brief に上げる。
    """

    def test_high_apt_analytical_routes_to_brief_not_watch(self) -> None:
        """high + apt が research スタイルでも brief (旧: R4 で watch に埋もれた)。"""
        s = _signals(importance="high", category="apt", article_type="research")
        d = route(s)
        assert d.channel == "brief"
        assert d.rule_id == "R3.5.high_threat_brief"

    def test_high_malware_opinion_routes_to_brief(self) -> None:
        s = _signals(importance="high", category="malware", article_type="opinion")
        d = route(s)
        assert d.channel == "brief"
        assert d.rule_id == "R3.5.high_threat_brief"

    def test_high_policy_research_stays_watch(self) -> None:
        """policy は threat カテゴリでないので R3.5 で昇格しない (R4 watch のまま)。"""
        s = _signals(importance="high", category="policy", article_type="research")
        d = route(s)
        assert d.channel == "watch"
        assert d.rule_id == "R4.watch_article_type_demote"

    def test_medium_apt_research_stays_watch(self) -> None:
        """medium は R3.5 対象外 (high のみ)。research style は R4 watch。"""
        s = _signals(importance="medium", category="apt", article_type="research")
        d = route(s)
        assert d.channel == "watch"
        assert d.rule_id == "R4.watch_article_type_demote"

    def test_high_apt_propaganda_still_demoted(self) -> None:
        """high + apt でも editorial_stance=propaganda なら R5b で watch を維持。"""
        s = _signals(
            importance="high",
            category="apt",
            article_type="breaking",
            editorial_stance="propaganda",
        )
        d = route(s)
        assert d.channel == "watch"
        assert d.rule_id == "R5b.propaganda_demote"


class TestGeopoliticalStaysOutOfCyberChannels:
    """Phase B-cal: geopolitical は importance によらず cyber チャンネル (alert/brief) に
    出さない。戦略的重要度で high になっても watch に留め、cyber トリアージを汚さない。
    """

    # 実 routing_signals は geopolitical を _SECURITY_RELEVANT_BROAD に含めないため
    # is_security_relevant=False になる。それを再現する。
    def test_geopolitical_high_routes_to_watch_not_brief(self) -> None:
        s = _signals(
            importance="high",
            category="geopolitical",
            article_type="breaking",
            is_security_relevant=False,
        )
        d = route(s)
        assert d.channel == "watch"
        assert d.channel not in ("alert", "brief")

    def test_geopolitical_medium_routes_to_watch(self) -> None:
        s = _signals(
            importance="medium",
            category="geopolitical",
            article_type="breaking",
            is_security_relevant=False,
        )
        d = route(s)
        assert d.channel == "watch"

    def test_research_high_routes_to_watch(self) -> None:
        s = _signals(
            importance="high",
            category="research",
            article_type="research",
            is_security_relevant=False,
        )
        d = route(s)
        assert d.channel == "watch"


class TestInoreaderRouting:
    def test_japan_critical_with_known_apt_routes_to_alert(self) -> None:
        s = _signals(
            category="incident",
            mentions_japan_critical=True,
            has_known_apt=True,
            article_type="breaking",
        )
        d = route(s)
        assert d.channel == "alert"
        assert d.rule_id == "R2.inoreader.alert_japan_critical_apt"

    def test_breaking_kev_routes_to_alert(self) -> None:
        s = _signals(
            category="vulnerability",
            article_type="breaking",
            has_kev_or_active_exploit=True,
        )
        d = route(s)
        assert d.channel == "alert"
        assert d.rule_id == "R2.inoreader.alert_breaking_kev"

    def test_breaking_zero_day_routes_to_alert(self) -> None:
        s = _signals(category="vulnerability", article_type="breaking", is_zero_day=True)
        d = route(s)
        assert d.channel == "alert"
        assert d.rule_id == "R2.inoreader.alert_breaking_kev"


class TestR2AlertCategoryGuard:
    """Phase B-cal2: R2 alert は cyber-threat category のみ適格。

    research/other/geopolitical は KEV/0day/APT 語が "話題として" 本文に出るだけで
    R2 を誤発火させ低重要度記事が alert に漏れていた (実測 arXiv 論文・TI サービス
    紹介文)。high_threat_brief_categories 外は alert に escalate させない。
    """

    def test_research_with_topical_zero_day_does_not_alert(self) -> None:
        # arXiv 論文が "ゼロデイ" を論じるだけ → is_zero_day=True だが alert 不可
        s = _signals(
            category="research",
            article_type="breaking",
            is_zero_day=True,
            importance="low",
        )
        d = route(s)
        assert d.channel != "alert"
        assert d.channel == "watch"

    def test_other_with_topical_kev_does_not_alert(self) -> None:
        # TI サービス紹介 (other) が "in-the-wild" を含むだけ → alert 不可
        s = _signals(
            category="other",
            article_type="breaking",
            has_kev_or_active_exploit=True,
            importance="low",
        )
        d = route(s)
        assert d.channel != "alert"

    def test_geopolitical_with_apt_mention_does_not_alert(self) -> None:
        s = _signals(
            category="geopolitical",
            article_type="breaking",
            mentions_japan_critical=True,
            has_known_apt=True,
            is_security_relevant=False,
        )
        d = route(s)
        assert d.channel != "alert"
        assert d.channel == "watch"

    def test_vulnerability_with_kev_still_alerts(self) -> None:
        # 本物の脅威カテゴリは従来どおり alert (回帰防止)
        s = _signals(
            category="vulnerability",
            article_type="breaking",
            has_kev_or_active_exploit=True,
        )
        d = route(s)
        assert d.channel == "alert"

    def test_recap_kev_does_not_alert(self) -> None:
        """recap は KEV を含んでも alert に行かない (Weekly Recap 問題の修正)。"""
        s = _signals(article_type="recap", has_kev_or_active_exploit=True)
        d = route(s)
        assert d.channel == "watch"
        assert d.rule_id == "R4.watch_article_type_demote"

    def test_top_n_does_not_alert(self) -> None:
        """Top N (tutorial) は重要度 high でも alert に行かない。"""
        s = _signals(article_type="tutorial", importance="high")
        d = route(s)
        assert d.channel == "watch"

    def test_japan_relevant_routes_to_japan_watch(self) -> None:
        s = _signals(is_japan_security_relevant=True, mentions_japan=True)
        d = route(s)
        assert d.channel == "japan_watch"
        assert d.rule_id == "R3.inoreader.japan_watch"

    def test_japan_recap_does_not_route_to_japan_watch(self) -> None:
        """日本関連でも recap なら watch (digest 系は japan_watch を汚染しない)。"""
        s = _signals(
            is_japan_security_relevant=True,
            mentions_japan=True,
            article_type="recap",
        )
        d = route(s)
        assert d.channel == "watch"

    def test_high_importance_security_routes_to_brief(self) -> None:
        s = _signals(importance="high", is_security_relevant=True)
        d = route(s)
        assert d.channel == "brief"
        assert d.rule_id == "R6.brief_importance"

    def test_low_importance_falls_through_to_watch(self) -> None:
        s = _signals(importance="low", is_security_relevant=False)
        d = route(s)
        assert d.channel == "watch"
        assert d.rule_id == "R7.fallback"

    def test_press_release_routes_to_watch(self) -> None:
        s = _signals(importance="high", article_type="press")
        d = route(s)
        assert d.channel == "watch"

    def test_opinion_routes_to_watch(self) -> None:
        s = _signals(importance="medium", article_type="opinion")
        d = route(s)
        assert d.channel == "watch"


# ---------- Grok 経路 ----------


class TestR8AptLeak:
    """Phase 5N: category=apt_leak → alert (PIR 3 直結)。"""

    def test_apt_leak_routes_to_alert(self) -> None:
        s = _signals(
            article_type="breaking",
            is_apt_leak=True,
        )
        d = route(s)
        assert d.channel == "alert"
        assert d.rule_id == "R8.alert_apt_leak"

    def test_apt_leak_advisory_routes_to_alert(self) -> None:
        """advisory タイプでも apt_leak なら alert (PIR 3 優先)。"""
        s = _signals(
            article_type="advisory",
            is_apt_leak=True,
        )
        d = route(s)
        assert d.channel == "alert"
        assert d.rule_id == "R8.alert_apt_leak"

    def test_apt_leak_recap_does_not_route_to_alert(self) -> None:
        """recap は apt_leak でも alert に行かない (週次まとめは即応対象外)。"""
        s = _signals(
            article_type="recap",
            is_apt_leak=True,
        )
        d = route(s)
        assert d.channel == "watch"  # R4 で降格

    def test_non_apt_leak_does_not_match_r8(self) -> None:
        """is_apt_leak=False なら R8 は発動しない。"""
        s = _signals(
            article_type="breaking",
            is_apt_leak=False,
        )
        d = route(s)
        assert d.rule_id != "R8.alert_apt_leak"


class TestRoutingDecisionAttachment:
    def test_decision_includes_signals_snapshot(self) -> None:
        """RoutingDecision に signals_snapshot が同梱され grep デバッグできる。"""
        s = _signals(article_type="breaking", has_kev_or_active_exploit=True)
        d = route(s)
        assert isinstance(d, RoutingDecision)
        assert d.signals_snapshot["article_type"] == "breaking"
        assert d.signals_snapshot["has_kev_or_active_exploit"] is True
        assert d.reason  # 非空


class TestRegexHierarchy:
    """Phase 5L-1 で _JAPAN_GENERAL_PATTERNS から汎用語を削除した検証。"""

    def test_kokunai_does_not_match_general_pattern(self) -> None:
        """LLM 要約に「国内外の組織」と書かれても japan general に match しない。"""
        from src.cti.routing_signals import _JAPAN_GENERAL_PATTERNS

        assert _JAPAN_GENERAL_PATTERNS.search("国内外の組織が影響") is None

    def test_critical_infrastructure_does_not_match_general_pattern(self) -> None:
        """汎用「重要インフラ」だけでは japan general に match しない。"""
        from src.cti.routing_signals import _JAPAN_GENERAL_PATTERNS

        assert _JAPAN_GENERAL_PATTERNS.search("重要インフラに影響を及ぼす") is None

    def test_japanese_does_not_match_japan_general(self) -> None:
        """JAPANESE のような単語境界違反で誤検知しない。"""
        from src.cti.routing_signals import _JAPAN_GENERAL_PATTERNS

        # 「JAPANESE」内の「JAPAN」に \b 単語境界で誤反応しない
        assert _JAPAN_GENERAL_PATTERNS.search("JAPANESE researchers") is None

    def test_japan_word_matches_general(self) -> None:
        """単独の Japan は match する (正常系)。"""
        from src.cti.routing_signals import _JAPAN_GENERAL_PATTERNS

        assert _JAPAN_GENERAL_PATTERNS.search("targeted Japan and Korea") is not None

    def test_nihongo_does_not_match_japan_general(self) -> None:
        """「日本語」「日本食」等は negative lookahead で除外。"""
        from src.cti.routing_signals import _JAPAN_GENERAL_PATTERNS

        assert _JAPAN_GENERAL_PATTERNS.search("日本語の記事") is None
        assert _JAPAN_GENERAL_PATTERNS.search("日本食レストラン") is None

    def test_japan_targeted_phrases_match_critical(self) -> None:
        """Phase 5O: 「日本標的を意味する強い表現」のみ critical regex に match する。

        Phase 5O 厳格化により、発信者・ベンダー名 (JPCERT/NISC/警察庁/トヨタ等) は
        critical regex から削除済。残るのは「攻撃事案」「被害組織」を強く示唆する
        表現のみ。
        """
        from src.cti.routing_signals import _JAPAN_CRITICAL_PATTERNS

        # 残した語 (Japan-targeted の確実な signal)
        for keyword in [
            "防衛省",
            "防衛装備",
            "自衛隊",
            "JSDF",
            "日本企業",
            "日本標的",
            "日本政府",
            "日系大手",
            "国内重要インフラ",
        ]:
            assert _JAPAN_CRITICAL_PATTERNS.search(f"foo {keyword} bar") is not None, (
                f"{keyword} should match critical patterns"
            )

    def test_publisher_and_vendor_names_no_longer_match_critical(self) -> None:
        """Phase 5O: 発信者・ベンダー名は critical regex に match しなくなる。

        これにより JPCERT/CC が ELECOM の advisory を発信した記事が
        japan_watch に流れる誤分類を防ぐ。
        """
        from src.cti.routing_signals import _JAPAN_CRITICAL_PATTERNS

        # Phase 5O で削除した語 (発信者 / 製品ベンダー)
        for keyword in [
            "JPCERT",
            "NISC",
            "警察庁",
            "NTT",
            "ソフトバンク",
            "KDDI",
            "楽天",
            "トヨタ",
            "ホンダ",
            "日産",
            "三菱",
            "三井",
            "住友",
        ]:
            assert _JAPAN_CRITICAL_PATTERNS.search(f"foo {keyword} bar") is None, (
                f"{keyword} should NOT match critical patterns (Phase 5O)"
            )


class TestSourceIdentityNoLongerAffectsRouting:
    """Phase B-cal: source 識別子による降格 (旧 R5b-fallback) を撤去。

    routing は完全 content-based。state-media でも source ではなく
    editorial_stance / category / importance で判定する。
    """

    def test_state_media_policy_non_propaganda_routes_by_content(self) -> None:
        """低信頼だった feed でも propaganda でなければ source で降格しない。"""
        s = _signals(
            importance="medium",
            article_type="breaking",
            category="policy",
            feed_title="Sputnik Globe (Russia)",
            editorial_stance="factual_report",
        )
        d = route(s)
        # source ではなく content (medium + security_relevant) で brief
        assert d.channel == "brief"
        assert d.rule_id == "R6.brief_importance"

    def test_state_media_propaganda_still_demoted_by_content(self) -> None:
        """propaganda は source 非依存の R5b で watch (content-based)。"""
        s = _signals(
            importance="medium",
            article_type="breaking",
            category="policy",
            feed_title="Sputnik Globe (Russia)",
            editorial_stance="propaganda",
        )
        d = route(s)
        assert d.channel == "watch"
        assert d.rule_id == "R5b.propaganda_demote"


class TestR5bPropagandaContentBased:
    """Phase B-R5b 改訂: editorial_stance による content-based propaganda 降格。

    source 識別子は使わず、LLM が rhetorical 性質 (一方的 framing) で判定する。
    """

    def test_propaganda_demotes_to_watch_regardless_of_source(self) -> None:
        """editorial_stance=propaganda なら importance=high でも watch。"""
        s = _signals(
            importance="high",
            article_type="breaking",
            is_security_relevant=True,
            editorial_stance="propaganda",
            feed_title="(any source)",
        )
        d = route(s)
        assert d.channel == "watch"
        assert d.rule_id == "R5b.propaganda_demote"

    def test_factual_report_from_state_media_not_demoted(self) -> None:
        """国営メディアでも factual_report なら降格しない (公平)。"""
        s = _signals(
            importance="high",
            article_type="breaking",
            is_security_relevant=True,
            editorial_stance="factual_report",
            feed_title="Sputnik Globe (Russia)",
            category="apt",
        )
        d = route(s)
        # R5b は発火しない。high + threat category なので R3.5 で brief へ
        # (Phase B-cal: high 脅威は style 降格より優先して brief)。
        assert d.channel == "brief"
        assert d.rule_id == "R3.5.high_threat_brief"

    def test_analytical_thinktank_not_demoted(self) -> None:
        """analytical (Crisis Group 等のシンクタンク) は降格対象外。"""
        s = _signals(
            importance="medium",
            article_type="deep_analysis",
            is_security_relevant=True,
            editorial_stance="analytical",
            feed_title="Crisis Group",
            category="policy",
        )
        d = route(s)
        assert d.channel == "brief"


class TestR6BriefCap:
    """Phase 5T-V: R6.cap_demote (brief 24h cap) の検証。"""

    def test_under_cap_routes_to_brief(self) -> None:
        s = _signals(
            importance="medium",
            article_type="breaking",
            brief_count_24h_snapshot=10,
            category="vulnerability",
        )
        d = route(s)
        assert d.channel == "brief"
        assert d.rule_id == "R6.brief_importance"

    def test_at_cap_medium_demoted_to_watch(self) -> None:
        """cap (15) 到達後、medium は watch 降格。"""
        s = _signals(
            importance="medium",
            article_type="breaking",
            brief_count_24h_snapshot=15,
            category="vulnerability",
        )
        d = route(s)
        assert d.channel == "watch"
        assert d.rule_id == "R6.cap_demote"

    def test_at_cap_high_still_routes_brief(self) -> None:
        """cap 到達後でも high importance は brief 維持 (重要 article 保護)。"""
        s = _signals(
            importance="high",
            article_type="breaking",
            brief_count_24h_snapshot=20,
            category="vulnerability",
        )
        d = route(s)
        assert d.channel == "brief"
        # Phase B-cal: high + threat は R3.5 で brief (cap より前に確定 = より堅牢)
        assert d.rule_id == "R3.5.high_threat_brief"


class TestRoutingLayerStructure:
    """B1: route() の役割別 3 層 (優先/衛生/importance) の契約。

    層の分離が挙動を変えないこと + 各層の責務境界を固定する。
    """

    def test_priority_returns_none_when_no_escalation(self) -> None:
        """優先層: escalation 該当なし (plain medium) は None を返し衛生/importance へ委譲。"""
        from src.cti.router import _route_priority, get_source_quality

        s = _signals(importance="medium", category="other", article_type="breaking")
        assert _route_priority(s, get_source_quality(), {}) is None

    def test_hygiene_returns_none_when_no_demote(self) -> None:
        """衛生層: 降格条件なし (clean breaking) は None を返し importance 層へ委譲。"""
        from src.cti.router import _apply_hygiene, get_source_quality

        s = _signals(importance="medium", category="incident", article_type="breaking")
        assert _apply_hygiene(s, get_source_quality(), {}) is None

    def test_high_threat_priority_beats_form_hygiene(self) -> None:
        """tier 保持: 高脅威 (R3.5, 優先層) が形式降格 (R4, 衛生層) より優先。"""
        from src.cti.router import _apply_hygiene, _route_priority, get_source_quality

        s = _signals(importance="high", category="apt", article_type="research")
        sq = get_source_quality()
        # 優先層が brief を確定 → 衛生層 (research→watch) には到達しない
        assert (d := _route_priority(s, sq, {})) is not None
        assert d.channel == "brief"
        # 衛生層単体なら form-demote で watch (= 順序がこれを救っている)
        hyg = _apply_hygiene(s, sq, {})
        assert hyg is not None and hyg.channel == "watch"

    def test_importance_layer_always_returns(self) -> None:
        """importance 層は必ず決定を返す (fallback 含む)。"""
        from src.cti.router import _route_importance, get_source_quality

        s = _signals(importance="low", category="other", article_type="breaking")
        assert _route_importance(s, get_source_quality(), {}) is not None


class TestSourceQualityDbFirst:
    """source_quality も DB 正・未保存時 yaml seed fallback (運用 config DB 化)。"""

    def test_db_value_preferred(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.storage.config_store as cstore
        from src.cti.router import _load_source_quality_db_first

        monkeypatch.setattr(
            cstore,
            "get_config",
            lambda key, **kw: {"brief_cap_24h": 99, "high_threat_brief_categories": ["apt"]},
        )
        sq = _load_source_quality_db_first()
        assert sq.brief_cap_24h == 99
        assert sq.high_threat_brief_categories == ("apt",)

    def test_yaml_fallback_when_db_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.storage.config_store as cstore
        from src.cti.router import _load_source_quality_db_first

        monkeypatch.setattr(cstore, "get_config", lambda key, **kw: None)
        sq = _load_source_quality_db_first()
        assert len(sq.high_threat_brief_categories) > 0  # yaml seed default

    def test_corrupt_db_value_degrades_to_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.storage.config_store as cstore
        from src.cti.router import _load_source_quality_db_first

        # 不正な型 (schema 違反) → seed に degrade
        monkeypatch.setattr(cstore, "get_config", lambda key, **kw: {"brief_cap_24h": "bad"})
        sq = _load_source_quality_db_first()
        assert isinstance(sq.brief_cap_24h, int)
