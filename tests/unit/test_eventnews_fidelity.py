"""固有情報の網羅率 (src/eventnews/fidelity.py、2026-09-26)。"""

from __future__ import annotations

from src.eventnews.fidelity import entity_coverage, salient_terms


def test_numbers_are_compared_by_value_across_notation() -> None:
    src = "No samples, just a countdown and a $12k price. 6.1TB claimed."
    cov = entity_coverage(src, "カウントダウンと 1.2 万ドルの価格表示、6.1TB のデータを保有と主張")
    assert cov.missing == () and cov.total == 2


def test_header_meta_dates_and_common_words_are_not_counted() -> None:
    src = (
        "[1] **見出し** (@Ransom_DB, 2026-08-15T23:03:58+00:00)\n"
        "We now watch Xpl0itrs. The Security team said security matters in August."
    )
    terms, nums = salient_terms(src)
    assert "xpl0itrs" in terms
    assert "ransom_db" not in terms and "security" not in terms and "august" not in terms
    assert not nums  # 日時の数字を拾わない


def test_missing_identifiers_are_listed() -> None:
    src = "Google fixed CVE-2026-19556 and CVE-2026-19557 in Chrome 151.0.7922.137."
    cov = entity_coverage(src, "Google は Chrome 151.0.7922.137 で CVE-2026-19556 を修正した")
    assert cov.missing == ("CVE-2026-19557",)
    assert cov.ratio is not None and 0.5 < cov.ratio < 1.0


def test_no_salient_terms_gives_no_ratio() -> None:
    assert entity_coverage("ただの日本語の文。", "要約").ratio is None
