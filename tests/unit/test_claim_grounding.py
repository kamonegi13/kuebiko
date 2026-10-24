"""claim の固有名詞の関門 (``claim_grounding``) — 2026-10-24 の DD 判定で出た誤検出の型を固定する。

fixture の名前はすべて架空 (実在の組織・製品名を書かない)。
"""

from __future__ import annotations

from src.assessment.claim_grounding import GroundingSources, check_grounding


def _src(text: str, attribution: str = "") -> GroundingSources:
    return GroundingSources(text=text, attribution=attribution)


class TestStillBlocksInvention:
    def test_a_cve_missing_from_every_source_is_a_violation(self) -> None:
        got = check_grounding(
            "ExampleCorp の CVE-2026-9999 が悪用された", _src("ExampleCorp が侵害")
        )

        assert got.violations == ("CVE-2026-9999",)

    def test_an_agency_and_technique_absent_from_the_sources_are_violations(self) -> None:
        got = check_grounding(
            "調査で JPCERT と SSRF の痕跡が確認された", _src("ExampleCorp 系列で不正アクセス")
        )

        assert got.violations == ("JPCERT", "SSRF")

    def test_a_katakana_name_absent_from_the_sources_is_a_violation(self) -> None:
        got = check_grounding("ワイパーを展開", _src("ある銀行で障害"))

        assert got.violations == ("ワイパー",)

    def test_a_source_name_is_not_accepted_as_a_body_fact(self) -> None:
        # 出典欄にだけある名前は違反ではないが、本文の裏付けにもならない (attribution へ)
        got = check_grounding("VendorLabs が報じた", _src("無関係", attribution="VendorLabs"))

        assert got.violations == ()
        assert got.attributions == ("VendorLabs",)


class TestNoLongerFlagsGrounded:
    def test_fullwidth_halfwidth_and_case_differences_match(self) -> None:
        got = check_grounding(
            "ＬｏｃｋＢｉｔ が CVE-2026-1234 を悪用", _src("lockbit と cve-2026-1234")
        )

        assert got.violations == ()

    def test_spacing_variants_match(self) -> None:
        got = check_grounding("Dark Web Informer は主張した", _src("DarkWebInformer の投稿"))

        assert got.violations == ()

    def test_generic_words_are_not_proper_nouns(self) -> None:
        claim = "フィッシングとサーバへのアクセスや API ・ SNS 経由のスキャンでゼロデイを悪用"

        assert check_grounding(claim, _src("ある障害")).violations == ()

    def test_a_compound_of_a_grounded_name_and_generic_words_is_grounded(self) -> None:
        got = check_grounding(
            "サンプルストアアプリ予約への侵入", _src("サンプルストア ID が漏えい")
        )

        assert got.violations == ()

    def test_a_compound_of_only_generic_words_is_ignored(self) -> None:
        assert check_grounding("リモートコード実行", _src("x")).violations == ()

    def test_katakana_rendering_of_a_latin_name_matches(self) -> None:
        got = check_grounding(
            "マサビスイッチは公表した", _src("MASABI SWITCH が EC 基盤の侵害を公表")
        )

        assert got.violations == ()

    def test_latin_rendering_of_a_katakana_name_matches(self) -> None:
        got = check_grounding("MASABI SWITCH disclosed", _src("マサビスイッチが公表"))

        assert got.violations == ()

    def test_a_dissimilar_katakana_name_is_not_rescued_by_unrelated_latin(self) -> None:
        got = check_grounding("ゼニスバンクが被害", _src("MASABI SWITCH が EC 基盤の侵害を公表"))

        assert got.violations == ("ゼニスバンク",)

    def test_a_name_only_in_the_body_prefix_is_grounded(self) -> None:
        # 呼び出し側が見出し・要約・本文冒頭を 1 本の text に連結して渡す
        got = check_grounding(
            "ExampleHolding が公表", _src("見出し 要約 本文冒頭 ExampleHolding 株式会社")
        )

        assert got.violations == ()


class TestAttribution:
    def test_name_found_only_in_the_source_field_is_recorded_not_a_violation(self) -> None:
        got = check_grounding(
            "LabX は侵害を報告した", _src("ある製品が侵害された", attribution="LabX Threat Intel")
        )

        assert got.violations == ()
        assert got.attributions == ("LabX",)

    def test_a_name_in_the_text_is_not_counted_as_attribution(self) -> None:
        got = check_grounding("LabX は報告", _src("LabX の報告", attribution="LabX"))

        assert got.attributions == ()

    def test_a_term_in_neither_remains_a_violation_next_to_an_attribution(self) -> None:
        got = check_grounding("LabX が CVE-2026-1 を報告", _src("無関係", attribution="LabX"))

        assert got.violations == ("CVE-2026-1",)
        assert got.attributions == ("LabX",)


class TestBasics:
    def test_lowercase_words_and_kanji_are_not_checked(self) -> None:
        assert check_grounding("a campaign hit the bank 銀行", _src("")).violations == ()

    def test_results_are_in_order_without_duplicates(self) -> None:
        got = check_grounding("Zeta と Alpha と Zeta", _src("無関係"))

        assert got.violations == ("Zeta", "Alpha")
