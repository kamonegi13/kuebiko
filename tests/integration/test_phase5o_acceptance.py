"""Phase 5O 受け入れテスト (japan_watch 精度回復)。

検証対象:
    - JPCERT/CC が発信者として登場する記事は japan_watch に流れない
    - 日本ベンダー製品 advisory (BUFFALO/ELECOM 等) は brief 経路
    - 真の Japan-targeted 事案 (日本企業の侵害、国内組織の漏洩) は japan_watch

⚠ 2026-08-18 (7af14fd): summarizer の routing_flags のうち **japan_targeted /
is_breaking_critical は返却ゼロ**と判明し、routing の判定条件から外した。fixture にも
**書かない** — 書くと「効いている」と誤読され、死んだ経路を固定するテストが生まれる
(実際にこのファイルの 2 件がそれで赤くなった)。現行で生きている signal は:

    日本標的  : metadata["victim_country_iso"] == "JP" (抽出側が入れる)
    日本の重要組織: 本文の CRITICAL regex (日本企業 / 日本政府 / 防衛省 等)
    アクター  : routing_flags["primary_actor_id"] (judgment 分類器が入れる)
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
                    "primary_actor_id": "",
                    "dedup_key": "elecom-lan-vuln",
                    "confidence": "high",
                },
                "article_type": "advisory",
            },
        )
        signals = extract_signals_from_briefing(msg, body_text="ELECOM 脆弱性")
        decision = route(signals)
        # 発信者としての言及だけの advisory は japan_watch に行かず brief 経路。
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
        """被害国=日本 で既知アクター不明の breach は japan_watch 行き。

        日本標的の実 signal は **抽出された victim_country_iso == "JP"**。
        既知アクターが無いので R2 は発火せず R3.japan_watch に降りる。
        """
        msg = _msg(
            title="国内の A 大学と附属病院、教員 PC 経由で患者の個人情報が漏洩のおそれ",
            bluf="A 大学で教員 PC が踏み台にされ、患者の個人情報が漏洩",
            summary="A 大学および附属病院は教員 PC 経由の不正アクセスにより...",
            importance="high",
            category="breach",  # 侵害事案
            metadata={
                "victim_country_iso": "JP",  # 抽出した被害国 = 日本標的の実 signal
                "routing_flags": {
                    "primary_actor_id": "",  # アクター帰属未確定
                    "dedup_key": "univ-clinical-data-breach",
                    "confidence": "high",
                },
                "article_type": "advisory",
            },
        )
        signals = extract_signals_from_briefing(msg, body_text="A 大学 附属病院 漏洩")
        decision = route(signals)
        assert decision.channel == "japan_watch"
        assert decision.rule_id == "R3.japan_watch"

    def test_japanese_company_apt_breach_routes_to_alert(self) -> None:
        """日本企業への既知アクターによる侵害は alert (R2)。

        R2 の発火条件は **CRITICAL regex + 既知アクター**。個別の社名は Phase 5O で
        CRITICAL 語から外している (「X 社が侵害された」と「X 社製ルーターの脆弱性」を
        文字列で区別できないため) ので、標的化を明示する語が本文側に要る。
        """
        msg = _msg(
            title="日本企業の海外子会社にランサムウェア攻撃、データ窃取の被害",
            bluf="日本企業の海外子会社が既知アクターによる侵入を受けた",
            summary="日本企業の海外子会社が不正アクセスを受け、データが窃取された...",
            importance="high",
            category="breach",
            metadata={
                "victim_country_iso": "JP",
                "routing_flags": {
                    "primary_actor_id": "qilin",  # judgment が確定した主題アクター
                    "dedup_key": "jp-subsidiary-ransomware",
                    "confidence": "high",
                },
                "article_type": "breaking",
            },
        )
        signals = extract_signals_from_briefing(
            msg,
            body_text="日本企業 海外子会社 ランサムウェア データ窃取",
        )
        decision = route(signals)
        # R2: japan_critical + known actor → alert
        assert decision.channel == "alert"
        assert decision.rule_id == "R2.alert_japan_critical_apt"

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
