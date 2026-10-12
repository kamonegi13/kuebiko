"""翻訳の文字化け決定論関門 (src/cti/translation_fidelity.py) のユニットテスト。

実例は data/eval/c2c_x_report.md の再現実験 (Gitea→Gite0 等) に基づく。
"""

from __future__ import annotations

from src.cti.translation_fidelity import (
    detect_garbling,
    find_garbled_identifiers,
    find_garbled_word_tokens,
    find_suspicious_numbers,
)

# ---------- find_garbled_word_tokens ----------


def test_detects_and_repairs_single_letter_substitution() -> None:
    source = "Gitea released version 28.0.0 fixing 20 vulnerabilities."
    output = "Gite0 はバージョン 28.0.0 をリリースし、20 件の脆弱性を修正しました。"
    repairs = find_garbled_word_tokens(source, output)
    assert len(repairs) == 1
    assert repairs[0].broken == "Gite0"
    assert repairs[0].repaired == "Gitea"


def test_detects_digit_inserted_into_product_name() -> None:
    source = "A vulnerability was found in Capacitor plugins."
    output = "Capac0itor プラグインに脆弱性が見つかりました。"
    repairs = find_garbled_word_tokens(source, output)
    assert len(repairs) == 1
    assert repairs[0].broken == "Capac0itor"
    assert repairs[0].repaired == "Capacitor"


def test_detects_short_acronym_corruption() -> None:
    source = "CISA issued a new directive today."
    output = "C2A は本日新しい指令を発行しました。"
    repairs = find_garbled_word_tokens(source, output)
    assert len(repairs) == 1
    assert repairs[0].broken == "C2A"
    assert repairs[0].repaired == "CISA"


def test_correct_translation_is_not_flagged() -> None:
    # 正しいカタカナ化 (原文の語がそのまま訳文に無くても問題なし)
    source = "The attacker used lateral movement techniques."
    output = "攻撃者は横展開の手法を使用しました。"
    assert find_garbled_word_tokens(source, output) == ()


def test_token_present_verbatim_is_not_flagged() -> None:
    source = "The flaw affects Capacitor and Gitea deployments."
    output = "この欠陥は Capacitor と Gitea の構成に影響します。"
    assert find_garbled_word_tokens(source, output) == ()


def test_ambiguous_candidates_are_not_repaired() -> None:
    # 編集距離内に複数の原文トークンが存在する場合は曖昧なので置換しない
    source = "Both Golem and Golex were affected by the issue."
    output = "Golen も影響を受けました。"
    assert find_garbled_word_tokens(source, output) == ()


def test_distance_too_large_is_not_repaired() -> None:
    # 長い語でも許容編集距離 (上限2) を超える崩れは検出しない (既知の限界)
    source = "ShinyHunters claimed responsibility for the breach."
    output = "ShinyHers がこの侵害の犯行声明を出しました。"
    assert find_garbled_word_tokens(source, output) == ()


def test_unrelated_short_english_word_not_flagged() -> None:
    source = "This report covers the incident in detail."
    output = "このレポートはインシデントの詳細を扱います。"
    assert find_garbled_word_tokens(source, output) == ()


# ---------- find_garbled_identifiers ----------


def test_detects_cve_year_digit_corruption() -> None:
    source = "The flaw is tracked as CVE-2026-76746."
    output = "この欠陥は CVE-2016-76746 として追跡されています。"
    repairs = find_garbled_identifiers(source, output)
    assert len(repairs) == 1
    assert repairs[0].kind == "cve"
    assert repairs[0].broken == "CVE-2016-76746"
    assert repairs[0].repaired == "CVE-2026-76746"


def test_correct_cve_is_not_flagged() -> None:
    source = "The flaw is tracked as CVE-2026-76746."
    output = "この欠陥は CVE-2026-76746 として追跡されています。"
    assert find_garbled_identifiers(source, output) == ()


def test_ambiguous_identifier_not_repaired_when_multiple_source_candidates() -> None:
    source = "Both CVE-2026-11111 and CVE-2026-11112 are affected."
    output = "CVE-2026-11119 が影響を受けます。"
    assert find_garbled_identifiers(source, output) == ()


# ---------- find_suspicious_numbers ----------


def test_detects_single_digit_substitution_in_number() -> None:
    # 2桁以上が対象 (1桁は「1つ」等の助数詞ノイズが支配的、data/eval/p1_report.md)
    source = "The group sanctioned 136 individuals last year."
    output = "このグループは昨年 130 人を制裁しました。"
    flags = find_suspicious_numbers(source, output)
    assert len(flags) == 1
    assert flags[0].broken == "130"
    assert flags[0].nearest_source == "136"


def test_detects_percentage_digit_substitution() -> None:
    source = "Ransom demands rose by 250% this quarter."
    output = "今四半期、ランサム要求は 200% 増加しました。"
    flags = find_suspicious_numbers(source, output)
    assert len(flags) == 1
    assert flags[0].broken == "200%"


def test_matching_number_is_not_flagged() -> None:
    source = "The group sanctioned 36 individuals last year."
    output = "このグループは昨年 36 人を制裁しました。"
    assert find_suspicious_numbers(source, output) == ()


def test_unrelated_new_number_not_flagged() -> None:
    # 原文に無い数字でも、原文の数字と近傍でなければ化けとみなさない
    source = "The report was published in October."
    output = "このレポートは 2026 年 10 月に公開されました。"
    assert find_suspicious_numbers(source, output) == ()


def test_detects_kana_interleaved_into_digit_sequence() -> None:
    source = "The exploit affects 71 products across vendors."
    output = "このエクスプロイトは複数のベンダーの 7な7 件の製品に影響します。"
    flags = find_suspicious_numbers(source, output)
    assert any(f.broken == "7な7" for f in flags)


def test_kanji_date_counter_is_not_flagged_as_interleaved() -> None:
    # 「3月4日」のような日付表現はカナではないため誤検出しない
    source = "The patch will be released soon."
    output = "パッチは 3月4日 にリリースされます。"
    assert find_suspicious_numbers(source, output) == ()


def test_number_with_multiple_close_candidates_flags_nearest() -> None:
    # 数字は自動修復しないため、識別子・語トークンと異なり一意性までは要求しない
    # (実測 data/eval/p1_report.md: 一意性を要求すると偶然近い無関係な数字が
    # 1 つ混ざるだけで検出自体が働かなくなるケースが大半だった)。
    source = "Figures of 100 and 102 were both reported."
    output = "101 件が報告されました。"
    flags = find_suspicious_numbers(source, output)
    assert len(flags) == 1
    assert flags[0].broken == "101"
    assert flags[0].nearest_source in ("100", "102")


def test_single_digit_number_is_not_flagged() -> None:
    # 1桁の数字は助数詞表現 ("1つ"/"3日間" 等) のノイズが支配的なため対象外
    source = "There were 3 suspects and 36 victims."
    output = "容疑者は 4 人、被害者は 36 人でした。"
    assert find_suspicious_numbers(source, output) == ()


# ---------- detect_garbling (統合、検出のみ) ----------


def test_detect_garbling_reports_word_identifier_and_number_without_rewriting() -> None:
    source = "Gitea fixed CVE-2026-76746 affecting 136 deployments, a 250% jump from last year."
    output = (
        "Gite0 は CVE-2016-76746 を修正し、130 件の構成に影響しました。"
        "これは昨年から 200% の増加です。"
    )
    report = detect_garbling(source, output)
    assert report.is_garbled
    assert len(report.word_hits) == 1
    assert len(report.identifier_hits) == 1
    assert len(report.number_flags) >= 2  # 130 / 200% (識別子内の桁も数字として拾う)
    assert report.total == 2 + len(report.number_flags)


def test_detect_garbling_clean_translation_is_not_garbled() -> None:
    source = "Gitea fixed a vulnerability affecting 36 deployments."
    output = "Gitea は 36 件の構成に影響する脆弱性を修正しました。"
    report = detect_garbling(source, output)
    assert not report.is_garbled
    assert report.total == 0
