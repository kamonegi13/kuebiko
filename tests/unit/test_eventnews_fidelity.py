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


def test_entity_gaps_lists_only_missing_and_tolerates_spacing_and_abbreviation() -> None:
    from src.eventnews.fidelity import entity_gaps

    entities = {
        "cve": ["CVE-2026-1", "CVE-2026-2"],
        "affected_vendor": ["checkpoint"],
        "affected_product": ["big-ip access policy manager"],
        "mentioned_country": ["US"],  # 訳語になる種類は照合しない
    }
    summary = "Check Point と F5 BIG-IP APM の CVE-2026-1 について"
    checked, missing = entity_gaps(entities, summary)
    assert checked == 4
    assert missing == [("cve", "CVE-2026-2")]


def test_weekly_line_warns_on_drop_and_needs_enough_versions() -> None:
    from src.eventnews.fidelity import Coverage, weekly_line

    def covs(ratio: float, n: int) -> list[Coverage]:
        return [Coverage(hit=int(ratio * 100), total=100, missing=()) for _ in range(n)]

    line, warn = weekly_line(covs(0.40, 20), covs(0.50, 20))
    assert warn and "40%" in line and "前週 50%" in line
    assert weekly_line(covs(0.49, 20), covs(0.50, 20))[1] is False
    assert weekly_line(covs(0.40, 3), covs(0.50, 20)) == (
        "事象ニュース 固有情報の網羅率: 版が少ない (3 版)",
        False,
    )
