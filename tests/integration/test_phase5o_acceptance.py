"""Phase 5O 受け入れテスト (japan_watch 精度回復)。

検証対象:
    - JPCERT/CC が発信者として登場する記事は japan_watch に流れない
    - 日本ベンダー製品 advisory (BUFFALO/ELECOM 等) は brief 経路
    - 真の Japan-targeted 事案 (デンソー侵害、東北大学漏洩) は japan_watch
"""

from __future__ import annotations

from src.cti.router import route
from src.cti.routing_signals import extract_signals_from_briefing
from src.tools.discord_publisher import BriefingMessage


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


class TestPhase5OAcceptance:
    """Phase 5O: japan_watch から発信者・ベンダー誤検知を排除する。"""

    def test_jpcert_publisher_advisory_routes_to_brief_not_japan_watch(self) -> None:
        """JPCERT/CC が ELECOM の vulnerability advisory を発信した記事は brief 行き。

        Phase 5O 修正対象の代表ケース。Phase 5N 観察期間 (5/7-8) に 53 件流入し
        japan_watch を汚染した typology の解消を検証。
        """
        msg = _msg(
            title="ELECOM 製無線 LAN ルーターに複数の脆弱性が存在",
            bluf="JPCERT/CC は ELECOM 製ルーターの脆弱性に対する advisory を公開",
            summary="JPCERT/CC は本日、ELECOM 製無線 LAN ルーターに複数の脆弱性...",
            importance="high",
            category="vulnerability",  # ベンダー製品脆弱性
            metadata={
                "routing_flags": {
                    "japan_targeted": False,  # LLM が正しく false 判定
                    "is_breaking_critical": False,
                    "primary_actor_id": "",
                    "dedup_key": "elecom-lan-vuln",
                    "confidence": "high",
                },
                "article_type": "advisory",
            },
        )
        signals = extract_signals_from_briefing(msg, body_text="ELECOM 脆弱性")
        decision = route(signals)
        # japan_targeted=False の発信者 advisory は japan_watch に行かず brief 経路。
        # (high vuln は R3.5.high_threat_brief が先に拾う。R6 ではない=phase_b_cal で R3.5 追加)
        assert decision.channel != "japan_watch"
        assert decision.channel == "brief"
        assert decision.rule_id == "R3.5.high_threat_brief"

    def test_buffalo_vendor_advisory_routes_to_brief(self) -> None:
        """日本ベンダー (BUFFALO) の製品 advisory は brief 経路。"""
        msg = _msg(
            title="BUFFALO 製 Wi-Fi ルーターに複数の脆弱性が存在",
            bluf="JVN は BUFFALO 製ルーターの脆弱性を公開",
            summary="BUFFALO の Wi-Fi ルーター製品で OS コマンドインジェクション脆弱性...",
            importance="medium",
            category="vulnerability",
            metadata={
                "routing_flags": {
                    "japan_targeted": False,
                    "is_breaking_critical": False,
                    "primary_actor_id": "",
                    "dedup_key": "buffalo-wifi-vuln",
                    "confidence": "high",
                },
                "article_type": "advisory",
            },
        )
        signals = extract_signals_from_briefing(msg, body_text="BUFFALO ルーター")
        decision = route(signals)
        assert decision.channel == "brief"

    def test_real_japan_breach_routes_to_japan_watch(self) -> None:
        """真の Japan-targeted 事案で APT 不明 + 進行中でない breach は japan_watch 行き。

        is_breaking_critical=False / 既知 APT 不明 のとき、R1.5 / R2 / R8 が
        いずれも発火せず R3.japan_watch に降りる。
        """
        msg = _msg(
            title="東北大学・東北大学病院、教員 PC 経由で治験患者の個人情報が漏洩のおそれ",
            bluf="東北大学で教員 PC が踏み台にされ、治験患者の個人情報が漏洩",
            summary="東北大学および東北大学病院は教員 PC 経由の不正アクセスにより...",
            importance="high",
            category="breach",  # 侵害事案
            metadata={
                "routing_flags": {
                    "japan_targeted": True,  # LLM が正しく true 判定
                    "is_breaking_critical": False,  # 進行中ではない (調査中段階)
                    "primary_actor_id": "",  # APT 帰属未確定
                    "dedup_key": "tohoku-univ-clinical-data-breach",
                    "confidence": "high",
                },
                "article_type": "advisory",  # breaking ではなく advisory 扱い
            },
        )
        signals = extract_signals_from_briefing(msg, body_text="東北大学 治験患者 漏洩")
        decision = route(signals)
        assert decision.channel == "japan_watch"
        assert decision.rule_id == "R3.inoreader.japan_watch"

    def test_denso_apt_breach_routes_to_alert(self) -> None:
        """日本企業 (デンソー) APT 侵害事案は alert (R2)。"""
        msg = _msg(
            title="デンソー海外子会社へ Qilin ランサムウェア侵入 (DENSO 6902)",
            bluf="デンソー海外子会社が Qilin ランサムウェアによる侵入を受けた",
            summary="デンソー (6902) の海外子会社が Qilin による不正アクセス・データ窃取...",
            importance="high",
            category="breach",
            metadata={
                "routing_flags": {
                    "japan_targeted": True,
                    "is_breaking_critical": True,
                    "primary_actor_id": "qilin",
                    "dedup_key": "qilin-denso-overseas-breach",
                    "confidence": "high",
                },
                "article_type": "breaking",
            },
        )
        signals = extract_signals_from_briefing(
            msg,
            body_text="デンソー 海外子会社 Qilin ランサムウェア",
        )
        decision = route(signals)
        # R2: japan_critical + known APT → alert
        assert decision.channel == "alert"
        assert decision.rule_id == "R2.inoreader.alert_japan_critical_apt"

    def test_global_cve_advisory_routes_to_brief(self) -> None:
        """グローバル製品の CVE advisory (Apache 等) は brief 行き、japan_watch には流れない。"""
        msg = _msg(
            title="Apache HTTP/2 の重大な脆弱性 CVE-2026-23918",
            bluf="Apache HTTP/2 実装に重大な脆弱性",
            summary="Apache HTTP/2 で DoS 攻撃と権限昇格に発展する可能性...",
            importance="high",
            category="vulnerability",
            metadata={
                "routing_flags": {
                    "japan_targeted": False,
                    "is_breaking_critical": True,
                    "primary_actor_id": "",
                    "dedup_key": "cve-2026-23918",
                    "confidence": "high",
                },
                "article_type": "advisory",
            },
        )
        signals = extract_signals_from_briefing(msg, body_text="Apache HTTP/2")
        decision = route(signals)
        assert decision.channel != "japan_watch"
        # vulnerability + advisory + high → brief
        assert decision.channel == "brief"

    def test_jpcert_publisher_mention_alone_does_not_match_critical_regex(self) -> None:
        """`JPCERT` という発信者言及だけでは critical regex に match しない (Phase 5O)。"""
        from src.cti.routing_signals import _JAPAN_CRITICAL_PATTERNS

        # Phase 5O 後: 発信者言及は match しない
        assert _JAPAN_CRITICAL_PATTERNS.search("JPCERT/CC は脆弱性を公開した") is None
        assert _JAPAN_CRITICAL_PATTERNS.search("NISC が報告した") is None
        assert _JAPAN_CRITICAL_PATTERNS.search("警察庁の発表") is None

        # Japan-targeted 強 signal は match する (正常系)
        assert _JAPAN_CRITICAL_PATTERNS.search("自衛隊への攻撃が観測された") is not None
        assert _JAPAN_CRITICAL_PATTERNS.search("日本企業が侵害された") is not None
        assert _JAPAN_CRITICAL_PATTERNS.search("防衛省への攻撃") is not None
