"""src.cti.law_enforcement_gate のテスト (R1b)。"""

from __future__ import annotations

import pytest

from src.cti.law_enforcement_gate import (
    GATE_ENV,
    has_law_enforcement_keyword,
    is_law_enforcement_gate_enabled,
    law_enforcement_probe_question,
    probe_law_enforcement_announcement,
)


class TestIsLawEnforcementGateEnabled:
    def test_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(GATE_ENV, raising=False)
        assert is_law_enforcement_gate_enabled() is False

    def test_disabled_when_explicit_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(GATE_ENV, "0")
        assert is_law_enforcement_gate_enabled() is False

    def test_enabled_when_set_to_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(GATE_ENV, "1")
        assert is_law_enforcement_gate_enabled() is True


class TestHasLawEnforcementKeyword:
    @pytest.mark.parametrize(
        "title",
        [
            "FBI・司法省、中国系 Flax Typhoon のハッキングツールを無力化",
            "米司法省、Flax Typhoon 関連の中国国家系ハッキングインフラ 7 ドメインを押収",
            "大学メール 31TB 窃取のイラン系ハッカー、米国で逮捕",
            "米検察、ランサムウェア運営者を起訴",
            "US DOJ announces takedown of botnet infrastructure",
            "Suspect indicted for ransomware operation",
            "Hacker arrested after cross-border investigation",
            "Authorities seized domains linked to the campaign",
            "Police disrupt phishing-as-a-service operation",
        ],
    )
    def test_detects_law_enforcement_titles(self, title: str) -> None:
        assert has_law_enforcement_keyword(title) is True

    @pytest.mark.parametrize(
        "title",
        [
            "新しいランサムウェア株、医療機関を標的に",
            "CISA が KEV カタログに 3 件の脆弱性を追加",
            "New zero-day exploited in the wild, patch now",
            "Critical RCE vulnerability found in widely used library",
        ],
    )
    def test_does_not_flag_unrelated_titles(self, title: str) -> None:
        assert has_law_enforcement_keyword(title) is False

    def test_checks_summary_too(self) -> None:
        assert has_law_enforcement_keyword("タイトルだけでは判定不能", "本文で逮捕者が出た") is True


class TestLawEnforcementProbeQuestion:
    def test_includes_title_and_summary(self) -> None:
        q = law_enforcement_probe_question("t1", "s1")
        assert "t1" in q
        assert "s1" in q
        assert "摘発" in q


class TestProbeLawEnforcementAnnouncement:
    @pytest.mark.asyncio
    async def test_returns_none_when_probe_choice_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _fake_probe_choice(*_args: object, **_kwargs: object) -> None:
            return None

        monkeypatch.setattr("src.cti.law_enforcement_gate.probe_choice", _fake_probe_choice)
        result = await probe_law_enforcement_announcement(
            title="t",
            summary="s",
            config=object(),  # type: ignore[arg-type]
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_p_yes_from_probe_choice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _fake_probe_choice(*_args: object, **_kwargs: object) -> dict[str, float]:
            return {"はい": 0.9, "いいえ": 0.1}

        monkeypatch.setattr("src.cti.law_enforcement_gate.probe_choice", _fake_probe_choice)
        result = await probe_law_enforcement_announcement(
            title="t",
            summary="s",
            config=object(),  # type: ignore[arg-type]
        )
        assert result == pytest.approx(0.9)
