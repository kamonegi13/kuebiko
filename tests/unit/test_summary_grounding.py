"""薄い Grok 投稿の要約の裏付け関門 (2026-10-24) のテスト。

本文が数百字の投稿で、要約が本文に無い数字・版番号・年・CVE を足す (s24c の重い誤り) のを
決定論で見つける。実例 (ESXi の版・修正日、JPCERT の年月日、攻撃者の帰属の「記載なし」) と、
誤発火しやすい型 (数詞の換算・投稿日の年・ハンドル名の数字) を固定する。
"""

from __future__ import annotations

import pytest

from src.pipeline.summary_grounding import (
    GATE_ENV,
    absent_actor_contradiction,
    gate_mode,
    ungrounded_dates,
    ungrounded_numbers,
    ungrounded_reasons,
)

_ESXI_BODY = (
    "The Great VM Escape: ESXi Exploitation in the Wild "
    "www.huntress.com/blog/esxi-vm-escape-exploit\n"
    "参考 URL: https://www.huntress.com/blog/esxi-vm-escape-exploit"
)


class TestUngroundedNumbers:
    def test_version_and_date_not_in_body_are_found(self) -> None:
        summary = (
            "悪用されたのは ESXi 7.0.2 で、VMware は 2023 年 11 月 22 日に 7.0.2u4 を公開した。"
        )

        found = ungrounded_numbers(summary, source=_ESXI_BODY)

        assert "7.0.2" in found
        assert "2023" in found

    def test_numbers_present_in_body_are_not_flagged(self) -> None:
        body = "after returning 15,000 BNB, 1,796.6 BNB in; ~40.8 ETH left"
        summary = "15,000 BNB を返還し、1,796.6 BNB が流入、約 40.8 ETH が残る。"

        assert ungrounded_numbers(summary, source=body) == ()

    def test_comma_and_fullwidth_differences_are_absorbed(self) -> None:
        assert ungrounded_numbers("被害は１，５００ GB。", source="Data Compromised: 1500 GB") == ()

    def test_trailing_zero_decimal_matches_integer(self) -> None:
        assert ungrounded_numbers("CVSS スコアは 10.0。", source="CVSS: 10") == ()

    def test_year_inside_url_digits_is_grounded(self) -> None:
        body = "JETRO https://news.web.nhk/newsweb/na/nd-20261019de61442"

        assert ungrounded_numbers("2026年10月19日に公表した。", source=body) == ()

    def test_publication_year_is_exempt(self) -> None:
        summary = "FalconFeeds.io は 2026 年 10 月 14 日に報告した。"

        assert ungrounded_numbers(summary, source="Reported on: 14 Oct") != ()
        assert ungrounded_numbers(summary, source="Reported on: 14 Oct", exempt_years=(2026,)) == ()

    def test_small_integers_are_ignored(self) -> None:
        assert ungrounded_numbers("4 件の被害組織、15 日以内。", source="victims") == ()

    def test_numbers_with_cjk_scale_are_ignored(self) -> None:
        # 「17 BILLION」を「170億ドル」、「Thousands」を「数千人」と換算して書くのは正当
        assert ungrounded_numbers("約170億ドル、約330万世帯。", source="$17 BILLION seized") == ()

    def test_k_suffix_in_body_grounds_thousands(self) -> None:
        assert ungrounded_numbers("45,000 ファイルを掲載。", source="67.5GB & 45K files") == ()

    def test_digits_inside_handles_are_ignored(self) -> None:
        assert ungrounded_numbers("PJ (@Npj8448) の投稿。", source="Patch now!") == ()

    def test_unpadded_date_part_still_requires_year(self) -> None:
        # 「n0n は 2024 年に活動を開始」のような背景知識の年は本文に無ければ違反
        found = ungrounded_numbers(
            "n0n は 2024 年初頭に活動を開始した。", source="n0n ransomware group"
        )

        assert found == ("2024",)


class TestUngroundedDates:
    def test_month_day_missing_from_body_is_found(self) -> None:
        body = "JPCERT/CC https://www.jpcert.or.jp/m/at/2026/at260030.html"

        assert ungrounded_dates("2026 年 3 月 30 日付で公表した。", source=body) == ("3/30",)

    def test_month_day_in_body_formats_is_grounded(self) -> None:
        assert ungrounded_dates("10月13日に公表。", source="[2026/10/13 09:00 公表]") == ()
        assert ungrounded_dates("10月9日に報告。", source="Reported on: 09 October, 2026") == ()

    def test_date_embedded_in_url_digits_is_grounded(self) -> None:
        body = "https://news.web.nhk/newsweb/na/nd-20261019de61442"

        assert ungrounded_dates("10月19日に公開停止。", source=body) == ()


class TestAbsentActorContradiction:
    def test_absence_claim_with_actor_field_in_body(self) -> None:
        summary = "本文には初期侵入経路、攻撃者の帰属、被害データの具体的な内容に関する記載はない。"
        body = "🔍 Key Details:\n🛡️ Threat Actor: CHAOS\n⚠️ Data Compromised: 1,500 GB"

        assert absent_actor_contradiction(summary, body) is True

    def test_absence_claim_without_actor_field_is_fine(self) -> None:
        summary = "攻撃者の帰属に関する記載はない。"

        assert absent_actor_contradiction(summary, "Hospital reported an outage.") is False

    def test_actor_field_without_absence_claim_is_fine(self) -> None:
        assert absent_actor_contradiction("CHAOS の被害に遭った。", "Threat Actor: CHAOS") is False


class TestUngroundedReasons:
    def test_reasons_cover_numbers_and_contradiction(self) -> None:
        reasons = ungrounded_reasons(
            "ESXi 7.0.2 が悪用された。攻撃者の帰属に関する記載はない。",
            body="Threat Actor: X\nESXi exploit",
        )

        assert any(r.startswith("number:7.0.2") for r in reasons)
        assert "absent_actor_contradicts_body" in reasons

    def test_clean_summary_has_no_reasons(self) -> None:
        assert ungrounded_reasons("CHAOS の被害に遭った。", body="CHAOS ransomware") == []


class TestGateMode:
    def test_default_is_shadow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(GATE_ENV, raising=False)

        assert gate_mode() == "shadow"

    @pytest.mark.parametrize("value", ["on", "off", "shadow"])
    def test_known_values(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(GATE_ENV, value)

        assert gate_mode() == value

    def test_unknown_value_falls_back_to_shadow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(GATE_ENV, "bogus")

        assert gate_mode() == "shadow"
