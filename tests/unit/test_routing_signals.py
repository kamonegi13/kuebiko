"""src.cti.routing_signals のテスト (Phase 5L-3: LLM フラグ駆動)。"""

from __future__ import annotations

from src.cti.routing_signals import extract_signals_from_briefing
from src.tools.discord_publisher import BriefingMessage


def _msg(
    *,
    title: str = "Sample Title",
    bluf: str = "BLUF text",
    summary: str = "summary text",
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


class TestHarvestedSignals:
    """語彙拡張 ① (回収): victim_sector / actor_nation を metadata から取り出す。"""

    def test_victim_sector_and_nations_from_metadata(self) -> None:
        msg = _msg(
            metadata={
                "victim_sector_canonical": "healthcare",
                "detected_actor_nations": ["cn", "ru"],
            },
        )
        signals = extract_signals_from_briefing(msg, body_text="本文")
        assert signals.victim_sector == "healthcare"
        assert signals.threat_actor_nations == frozenset({"cn", "ru"})

    def test_defaults_when_metadata_absent(self) -> None:
        signals = extract_signals_from_briefing(_msg(), body_text="本文")
        assert signals.victim_sector == ""
        assert signals.threat_actor_nations == frozenset()


class TestLlmFlagsPrimary:
    def test_llm_high_confidence_overrules_regex_japan_false_positive(self) -> None:
        """LLM が japan_targeted=False (high) なら regex の汎用語誤検知を overrule。

        Phase 5L-1 で「国内」を regex から外したが、なお残る regex 由来の誤検知
        (例: 「日本の研究者が解説」) を LLM の判断で打ち消す検証。
        """
        body = "Linux kernel CVE。日本の研究者が解説した。"  # regex で「日本」match
        msg = _msg(
            metadata={
                "routing_flags": {
                    "japan_targeted": False,
                    "is_breaking_critical": True,
                    "primary_actor_id": "",
                    "dedup_key": "cve-2026-31431",
                    "confidence": "high",
                },
            },
        )
        signals = extract_signals_from_briefing(msg, body_text=body)
        # LLM が False と言っているので regex に勝つ
        assert signals.mentions_japan_critical is False
        # LLM フラグが metadata に保持されている
        assert signals.llm_japan_targeted is False
        assert signals.llm_is_breaking_critical is True
        assert signals.llm_dedup_key == "cve-2026-31431"
        assert signals.llm_confidence == "high"

    def test_llm_low_confidence_falls_back_to_regex(self) -> None:
        """LLM confidence=low なら regex を採用 (フォールバック)。"""
        body = "JPCERT が新たな日本企業侵害を報告した。"
        msg = _msg(
            metadata={
                "routing_flags": {
                    "japan_targeted": False,  # LLM は False と言っているが confidence=low
                    "is_breaking_critical": False,
                    "primary_actor_id": "",
                    "dedup_key": "",
                    "confidence": "low",
                },
            },
        )
        signals = extract_signals_from_briefing(msg, body_text=body)
        # LLM 不確実なので regex フォールバック → JPCERT が critical に hit
        assert signals.mentions_japan_critical is True

    def test_llm_high_japan_targeted_true_drives_critical(self) -> None:
        """LLM が japan_targeted=True (high) で日本標的判定が立つ。"""
        msg = _msg(
            category="apt",
            metadata={
                "routing_flags": {
                    "japan_targeted": True,
                    "is_breaking_critical": True,
                    "primary_actor_id": "salt-typhoon",
                    "dedup_key": "salt-typhoon-jp-defense",
                    "confidence": "high",
                },
            },
        )
        signals = extract_signals_from_briefing(msg, body_text="活動報告")
        assert signals.mentions_japan_critical is True
        assert signals.has_known_apt is True
        assert signals.llm_primary_actor_id == "salt-typhoon"

    def test_no_routing_flags_uses_regex_only(self) -> None:
        """LLM が routing_flags を出力しなかった場合、regex のみで判定。"""
        msg = _msg(
            metadata={},  # routing_flags 無し
        )
        # Phase 5O: 「日本企業が侵害された」のような Japan-targeted 表現で regex match
        # (Phase 5O 以前は「JPCERT が報告」でも match したが、発信者 regex は削除済)
        signals = extract_signals_from_briefing(msg, body_text="日本企業が侵害された")
        # LLM フラグはすべて defensive default
        assert signals.llm_japan_targeted is False
        assert signals.llm_confidence == "low"
        # regex は機能する (新 critical pattern: 「日本企業」)
        assert signals.mentions_japan_critical is True

    def test_llm_invalid_routing_flags_falls_back_to_regex(self) -> None:
        """LLM が壊れた routing_flags を返しても crash せず regex フォールバック。"""
        msg = _msg(
            metadata={
                "routing_flags": "not a dict",  # 型違反
            },
        )
        # crash しない
        signals = extract_signals_from_briefing(msg, body_text="自衛隊への攻撃が観測された")
        assert signals.llm_confidence == "low"
        # regex の判定は維持される (自衛隊は Phase 5O 後も critical pattern)
        assert signals.mentions_japan_critical is True


class TestJapanWatchPrecision:
    """2026-06-17: japan_watch 誤分類修正。is_japan_security_relevant は『日本が実際の
    標的/被害』(LLM japan_targeted or victim_country=JP) のみ true。単なる言及では false。"""

    def _flags(self, *, japan_targeted: bool, confidence: str = "high") -> dict[str, object]:
        return {
            "routing_flags": {
                "japan_targeted": japan_targeted,
                "is_breaking_critical": False,
                "primary_actor_id": "",
                "dedup_key": "k",
                "confidence": confidence,
            }
        }

    def test_korea_breach_with_japan_mention_not_relevant(self) -> None:
        # クーパン型: victim=KR、本文に「日本企業も注意」で『日本企業』混入、LLM=日本標的false
        msg = _msg(
            category="breach",
            summary="韓国クーパンが漏洩。日本企業も同様のリスクに注意",
            metadata={**self._flags(japan_targeted=False), "victim_country_iso": "KR"},
        )
        sig = extract_signals_from_briefing(msg, body_text="韓国の大規模漏洩")
        assert sig.is_japan_security_relevant is False  # regex で日本企業に当たっても false

    def test_us_breach_japan_mention_not_relevant(self) -> None:
        # FBI/Outsider 型: 米事案で日本が一言、victim_country なし、japan_targeted false
        msg = _msg(
            category="breach",
            summary="FBIがフィッシングサービスを摘発。日本にも影響",
            metadata={**self._flags(japan_targeted=False)},
        )
        sig = extract_signals_from_briefing(msg, body_text="米国の摘発")
        assert sig.is_japan_security_relevant is False

    def test_advisory_with_japan_mention_not_relevant(self) -> None:
        msg = _msg(
            category="advisory",
            summary="OpenSSL advisory 公開。日本のミラーも更新",
            metadata={**self._flags(japan_targeted=False)},
        )
        sig = extract_signals_from_briefing(msg, body_text="OpenSSL")
        assert sig.is_japan_security_relevant is False

    def test_genuine_japan_target_via_llm_flag(self) -> None:
        msg = _msg(
            category="breach",
            summary="日本企業X社が不正アクセスで顧客情報漏洩",
            metadata={**self._flags(japan_targeted=True)},
        )
        sig = extract_signals_from_briefing(msg, body_text="X社侵害")
        assert sig.is_japan_security_relevant is True

    def test_genuine_japan_target_via_victim_country(self) -> None:
        # LLM confidence 低でも victim_country=JP なら japan_watch (ground truth)
        msg = _msg(
            category="incident",
            summary="国内組織で情報流出",
            metadata={
                **self._flags(japan_targeted=False, confidence="low"),
                "victim_country_iso": "JP",
            },
        )
        sig = extract_signals_from_briefing(msg, body_text="流出")
        assert sig.is_japan_security_relevant is True

    def test_accidental_misdelivery_excluded(self) -> None:
        # 誤送信 (事故・運用ミス) は JP 被害でも japan_watch から除外 (攻撃でない)
        msg = _msg(
            title="スポーツ教室当選者宛てメールで誤送信 - 取消機能で再発",
            category="breach",
            summary="自治体がメールを誤送信し個人情報が漏洩",
            metadata={**self._flags(japan_targeted=True), "victim_country_iso": "JP"},
        )
        sig = extract_signals_from_briefing(msg, body_text="誤送信により漏洩")
        assert sig.is_japan_security_relevant is False

    def test_loss_incident_excluded(self) -> None:
        msg = _msg(
            category="incident",
            summary="USB メモリを紛失し個人情報が流出した",
            metadata={**self._flags(japan_targeted=True), "victim_country_iso": "JP"},
        )
        sig = extract_signals_from_briefing(msg, body_text="紛失")
        assert sig.is_japan_security_relevant is False

    def test_accidental_keyword_but_attack_kept(self) -> None:
        # 「設定ミスを突かれて攻撃」: 攻撃語があれば事故扱いせず japan_watch 維持
        msg = _msg(
            category="breach",
            summary="設定ミスを悪用され不正アクセスで侵害された",
            metadata={**self._flags(japan_targeted=True), "victim_country_iso": "JP"},
        )
        sig = extract_signals_from_briefing(msg, body_text="不正アクセス")
        assert sig.is_japan_security_relevant is True

    def test_genuine_attack_not_treated_as_accident(self) -> None:
        # 攻撃由来の JP breach は維持 (事故語なし)
        msg = _msg(
            category="breach",
            summary="ランサムウェア攻撃で日本企業が侵害",
            metadata={**self._flags(japan_targeted=True), "victim_country_iso": "JP"},
        )
        sig = extract_signals_from_briefing(msg, body_text="ランサム")
        assert sig.is_japan_security_relevant is True


class TestLooksAccidentalLeak:
    """抽出した text-only ヘルパ (脅威マップ #8 と japan_watch で共用)。"""

    def test_accidental_keywords_true(self) -> None:
        from src.cti.routing_signals import looks_accidental_leak

        cases = ["メールを誤送信し漏洩", "USBメモリを紛失", "設定ミスで誤公開", "宛先間違いで流出"]
        for t in cases:
            assert looks_accidental_leak(t) is True, t

    def test_attack_word_overrides_accident(self) -> None:
        from src.cti.routing_signals import looks_accidental_leak

        # 攻撃語があれば事故扱いしない
        assert looks_accidental_leak("設定ミスを突かれ不正アクセスで侵害") is False
        assert looks_accidental_leak("誤設定をランサムウェアに悪用された") is False

    def test_plain_attack_and_empty_false(self) -> None:
        from src.cti.routing_signals import looks_accidental_leak

        assert looks_accidental_leak("ランサムウェア攻撃で侵害") is False
        assert looks_accidental_leak("") is False
        assert looks_accidental_leak("通常のセキュリティ更新") is False
