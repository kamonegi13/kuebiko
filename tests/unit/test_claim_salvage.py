"""削ってから照合し直す (DETECT_CLAIM_SALVAGE の部品)。付け足しの節だけ削り、通らなければ None。"""

from __future__ import annotations

from src.assessment.claim_fidelity import check_fidelity
from src.assessment.claim_grounding import GroundingSources, check_grounding
from src.assessment.claim_salvage import MIN_SALVAGED_CHARS, salvage_claim


def _src(text: str, attribution: str = "") -> GroundingSources:
    return GroundingSources(text=text, attribution=attribution, members=((text, ""),))


class TestSalvage:
    def test_drops_the_second_sentence(self) -> None:
        sources = _src("ホテルが不正アクセスを受け顧客情報が流出したと報じられている")
        claim = (
            "ホテルが不正アクセスを受け顧客情報が流出したと報じられている。"
            "事象は2026年9月9日に検知された。"
        )

        got = salvage_claim(claim, sources)

        assert got is not None
        assert got.claim == "ホテルが不正アクセスを受け顧客情報が流出したと報じられている。"
        assert got.removed == ("second_sentence",)

    def test_replaces_a_named_outlet_clause_with_a_plain_hedge(self) -> None:
        sources = _src("ホテルの顧客情報が流出", attribution="BleepingComputer")
        claim = "ホテルの顧客情報が流出したと BleepingComputer が報じた。"

        got = salvage_claim(claim, sources)

        assert got is not None
        assert got.claim == "ホテルの顧客情報が流出したと報じられている。"
        assert got.removed == ("outlet",)

    def test_according_to_prefix_keeps_the_hedge(self) -> None:
        sources = _src("ホテルの顧客情報が流出", attribution="SecurityWeek")
        claim = "SecurityWeek によると、ホテルの顧客情報が流出した。"

        got = salvage_claim(claim, sources)

        assert got is not None
        assert got.claim == "ホテルの顧客情報が流出したと報じられている。"

    def test_a_subject_named_in_the_headline_is_not_treated_as_an_outlet(self) -> None:
        # 見出しに名前がある主体 (出典欄にも同名) は出典名の付け足しではない = 削る対象が無い
        sources = _src("Kaspersky が未知の APT による攻撃を報告した", attribution="Kaspersky")
        claim = "Kaspersky が未知の APT による攻撃を報告したと報じた。"

        assert salvage_claim(claim, sources) is None

    def test_drops_a_resurfaced_parenthetical(self) -> None:
        sources = _src("ホテルの顧客情報が流出した事案")
        claim = "ホテルの顧客情報が流出した事案が確認された(旧事案が再浮上)。"

        got = salvage_claim(claim, sources)

        assert got is not None
        assert "再浮上" not in got.claim
        assert got.removed == ("unsupported:再浮上",)

    def test_resurfaced_in_the_predicate_is_not_salvaged(self) -> None:
        sources = _src("決済サービスに不正アクセスがあった")
        claim = "決済サービスに不正アクセスがあった事案が再浮上した。"

        assert salvage_claim(claim, sources) is None

    def test_drops_a_date_that_the_sources_do_not_carry(self) -> None:
        sources = _src("AWS が環境ツールの脆弱性を公表した")
        claim = "AWS が環境ツールの脆弱性を2026年10月28日に公表した。"

        got = salvage_claim(claim, sources)

        assert got is not None
        assert got.claim == "AWS が環境ツールの脆弱性を公表した。"
        assert got.removed == ("date",)

    def test_keeps_a_date_the_sources_carry(self) -> None:
        sources = _src("AWS が10月28日に環境ツールの脆弱性を公表した")

        assert salvage_claim("AWS が10月28日に環境ツールの脆弱性を公表した。", sources) is None

    def test_result_passes_both_gates(self) -> None:
        sources = _src("ホテルが不正アクセスを受け顧客情報が流出した")
        claim = "ホテルが不正アクセスを受け顧客情報が流出した。同社は10月3日に公表した。"

        got = salvage_claim(claim, sources)

        assert got is not None
        assert check_fidelity(got.claim, sources) == ()
        assert check_grounding(got.claim, sources).violations == ()

    def test_too_short_remainder_is_rejected(self) -> None:
        sources = _src("障害")
        claim = "障害。そのほかの付け足しが続く。"

        assert len("障害") < MIN_SALVAGED_CHARS
        assert salvage_claim(claim, sources) is None

    def test_a_remainder_that_lost_its_subject_is_rejected(self) -> None:
        sources = _src("流出した", attribution="SecurityWeek")
        claim = "SecurityWeek は、と報じた。"

        assert salvage_claim(claim, sources) is None

    def test_nothing_to_fix_returns_none(self) -> None:
        sources = _src("ホテルの顧客情報が流出した")

        assert salvage_claim("ホテルの顧客情報が流出した。", sources) is None

    def test_a_violation_that_survives_the_cut_is_not_salvaged(self) -> None:
        # 1 文目に本文に無い固有名詞 (LockBit) があるので、2 文目を削っても通らない
        sources = _src("ホテルの顧客情報が流出した件が報じられている")
        claim = "LockBit がホテルの顧客情報を流出させたと報じられている。事象は10月3日。"

        assert salvage_claim(claim, sources) is None
