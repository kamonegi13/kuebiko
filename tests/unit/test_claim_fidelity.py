"""claim の忠実さの関門と単発の事案の絞り込み (DETECT-WRITER-FIX)。

fixture は DETECT-JUDGE の Opus 裁定で誤りとされた型の**言い換え** (実在の組織名は使わない)。
これらは関門の**調整用**であり、再判定 (新しい窓・Opus) の根拠には使わない。
"""

from __future__ import annotations

import asyncio

import pytest

from src.assessment.claim_fidelity import check_fidelity, one_off_reason
from src.assessment.claim_grounding import GroundingSources
from src.assessment.detect_decompose import WrittenClaim, decompose_candidates, writer_gates_mode


def _src(
    summary: str, *, title: str = "見出し", feed: str = "", body: str = ""
) -> GroundingSources:
    return GroundingSources(
        text=f"{title} {summary} {body}", attribution=feed, members=((title, summary),)
    )


class TestCheckFidelity:
    def test_two_sentence_claim_is_flagged(self) -> None:
        src = _src("取引所A社が攻撃を認め資金が流出した。")
        got = check_fidelity("A社が攻撃を認めた。B社は資金を凍結したと公表した。", src)

        assert "multi_sentence" in got

    def test_single_sentence_with_trailing_period_is_clean(self) -> None:
        assert (
            check_fidelity("A社が攻撃を認めたと報じられている。", _src("A社が攻撃を認めた")) == ()
        )

    def test_month_day_absent_from_sources_is_flagged(self) -> None:
        src = _src("C社は10月23日に報告を受領し、10月30日に公表した")
        got = check_fidelity("C社は10月21日に不正アクセスを検知した。", src)

        assert got == ("date:10月21日",)

    def test_month_day_present_in_sources_passes(self) -> None:
        src = _src("C社は10月30日に公表した")
        assert check_fidelity("C社が10月30日に公表した", src) == ()

    def test_resurfacing_phrase_not_in_sources_is_flagged(self) -> None:
        got = check_fidelity("D社の侵害が再浮上した", _src("D社が不正アクセスを公表"))

        assert got == ("unsupported:再浮上",)

    def test_outlet_name_added_to_claim_is_flagged(self) -> None:
        src = _src("E社の顧客情報が侵害された", feed="Example Breach Watch")
        got = check_fidelity("E社の顧客情報が侵害されたとBreachwatchが報じた", src)
        assert got == ()  # 部分一致しない名前は対象外 (誤検出を避ける)

        got = check_fidelity("E社の顧客情報が侵害されたとExample Breach Watchが報じた", src)
        assert "outlet:examplebreachwatch" in got

    def test_outlet_word_in_headline_is_not_flagged(self) -> None:
        src = _src("E社の顧客情報が侵害された", title="Watchdog 報告", feed="Watchdog")
        assert check_fidelity("Watchdog によると E社が侵害された", src) == ()


class TestOneOffReason:
    def test_all_members_takedown_is_one_off(self) -> None:
        members = (("ボットネットを摘発", "警察が基盤を阻止した"),)
        assert one_off_reason(members) == "law_enforcement"

    def test_mixed_cluster_is_not_one_off(self) -> None:
        members = (("ボットネットを摘発", ""), ("新しい亜種を確認", "別の手口"))
        assert one_off_reason(members) is None

    def test_plain_incident_is_not_one_off(self) -> None:
        assert one_off_reason((("F社に不正アクセス", "個人情報が漏えいした可能性"),)) is None

    def test_empty_members(self) -> None:
        assert one_off_reason(()) is None


def _run(
    sources: GroundingSources, claim: str, *, enforce: bool
) -> tuple[list[str], tuple[str, ...]]:
    calls: list[str] = []

    async def write(cluster: object) -> WrittenClaim | None:
        calls.append("w")
        return WrittenClaim(claim=claim, domain="cyber_incident", skip_reason="")

    result = asyncio.run(
        decompose_candidates(
            candidate_ids=["a1"],
            ml_scores={"a1": 0.9},
            strong_by_aid={"a1": frozenset()},
            items_by_aid={},
            vec_by_aid=None,
            merge_for=lambda c: None,
            write_claim=write,
            grounding_text=lambda c: sources,
            enforce_grounding=enforce,
        )
    )
    out = result.outcomes[0]
    return [out.action, *calls], out.fidelity


class TestModeWiring:
    def test_default_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DETECT_WRITER_GATES", raising=False)
        assert writer_gates_mode() == "0"

    def test_off_keeps_current_behaviour(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DETECT_WRITER_GATES", raising=False)
        got, fidelity = _run(_src("G社が侵害された"), "G社が侵害された。再浮上した。", enforce=True)

        assert got == ["open", "w"]
        assert fidelity == ()

    def test_shadow_records_but_opens(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DETECT_WRITER_GATES", "shadow")
        got, fidelity = _run(_src("G社が侵害された"), "G社が侵害された。再浮上した。", enforce=True)

        assert got[0] == "open"
        assert "multi_sentence" in fidelity

    def test_on_blocks_bad_claim_when_enforcing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DETECT_WRITER_GATES", "1")
        got, fidelity = _run(_src("G社が侵害された"), "G社の侵害が再浮上した", enforce=True)

        assert got[0] == "skip"
        assert fidelity == ("unsupported:再浮上",)

    def test_on_without_enforcement_only_records(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DETECT_WRITER_GATES", "1")
        got, fidelity = _run(_src("G社が侵害された"), "G社の侵害が再浮上した", enforce=False)

        assert got[0] == "open"
        assert fidelity == ("unsupported:再浮上",)

    def test_on_skips_one_off_before_calling_the_writer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DETECT_WRITER_GATES", "1")
        src = _src("警察が基盤を阻止", title="ボットネットを摘発")
        got, fidelity = _run(src, "警察がボットネットを摘発した", enforce=True)

        assert got == ["skip"]  # 書き手は呼ばれない
        assert fidelity == ("one_off:law_enforcement",)

    def test_on_does_not_block_without_enforcement_for_one_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 旗 1 でも enforce_grounding=False (DETECT_DECOMPOSE=shadow) では止めず記録だけ
        monkeypatch.setenv("DETECT_WRITER_GATES", "1")
        src = _src("警察が基盤を阻止", title="ボットネットを摘発")
        got, fidelity = _run(src, "警察がボットネットを摘発した", enforce=False)

        assert got == ["open", "w"]
        assert fidelity == ("one_off:law_enforcement",)


class TestDateNormalisation:
    @pytest.mark.parametrize(
        "evidence",
        [
            "Published Nov 3 by the vendor",
            "November 3rd update",
            "3 November 2026",
            "2026-11-03",
            "11/3",
        ],
    )
    def test_equivalent_date_notations_in_sources_pass(self, evidence: str) -> None:
        assert check_fidelity("H社が11月3日に公表した", _src(evidence)) == ()

    def test_different_date_is_still_flagged(self) -> None:
        got = check_fidelity("H社が11月4日に公表した", _src("Published Nov 3"))
        assert got == ("date:11月4日",)


class TestSentenceBreak:
    def test_period_before_closing_bracket_is_not_a_break(self) -> None:
        claim = "H社は「侵害を確認した。」と述べたと報じられている。"
        assert "multi_sentence" not in check_fidelity(claim, _src("H社が侵害を確認"))

    def test_period_followed_by_text_is_a_break(self) -> None:
        assert "multi_sentence" in check_fidelity("甲が起きた。乙も起きた。", _src("甲 乙"))
