"""横断パターンのまとめ (決定論・純粋関数)。"""

from __future__ import annotations

from src.storage.repo_judgment_attrs import EventHeadline, JudgmentAttrs
from src.synthesis.grounded.estimate import KeyJudgment
from src.synthesis.grounded.patterns import build_patterns
from tests.unit.synthesis_pattern_fixtures import BREACH, EXPLOIT, JP, attrs, j


def _jp_breaches() -> tuple[list[KeyJudgment], dict[str, JudgmentAttrs]]:
    js = [
        j("s-1", "アルファ証券で顧客情報が漏えいした。詳細は調査中。"),
        j("s-2", "ベータ銀行で委託先経由の漏えいがあった。"),
        j("s-3", "ガンマ商事で侵害が確認された。"),
    ]
    at = {
        "s-1": attrs(countries=JP, kinds=BREACH, s3=True),
        "s-2": attrs(countries=JP, kinds=BREACH),
        "s-3": attrs(countries=JP, kinds=BREACH),
    }
    return js, at


def test_groups_by_region_and_kind_with_marks() -> None:
    js, at = _jp_breaches()
    plan = build_patterns(js, at)
    assert len(plan.patterns) == 1
    p = plan.patterns[0]
    assert (p.pattern_id, p.region, p.kind, p.count) == ("P1", "日本", "breach", 3)
    assert (p.s3_count, p.weight) == (1, 5)
    assert dict(p.delta_counts) == {"opened": 3}
    assert plan.rest == ()


def test_singleton_key_goes_to_rest() -> None:
    js, at = _jp_breaches()
    js.append(j("s-4", "デルタ社の脆弱性が悪用された。"))
    at["s-4"] = attrs(kinds=EXPLOIT)
    plan = build_patterns(js, at)
    assert [c.judgment_ids for c in plan.rest] == [("s-4",)]


def test_missing_attrs_or_kind_is_rest() -> None:
    plan = build_patterns(
        [j("s-1", "属性なしの動き。"), j("s-2", "助言のみ。")],
        {"s-2": attrs(kinds=frozenset({"advisory"}))},
    )
    assert plan.patterns == ()
    assert len(plan.rest) == 2


def test_judgments_sharing_event_root_merge_into_one_case() -> None:
    js = [
        j("s-1", "エプシロン社で侵害。"),
        j("s-2", "エプシロン社の侵害の続報。"),
        j("s-3", "ゼータ社で侵害。"),
    ]
    at = {
        "s-1": attrs(kinds=BREACH, roots=("ev-1",)),
        "s-2": attrs(kinds=BREACH, roots=("ev-1", "ev-9")),
        "s-3": attrs(kinds=BREACH, roots=("ev-3",)),
    }
    plan = build_patterns(js, at)
    p = plan.patterns[0]
    assert p.count == 2  # s-1 と s-2 は 1 事案
    merged = next(c for c in p.cases if len(c.judgment_ids) == 2)
    assert set(merged.judgment_ids) == {"s-1", "s-2"}
    assert len(merged.claims) == 2


def test_japan_first_then_weight() -> None:
    js = [j(f"s-{n}", f"事案{n}") for n in range(1, 7)]
    at = {
        "s-1": attrs(countries=JP, kinds=BREACH),
        "s-2": attrs(countries=JP, kinds=BREACH),
        "s-3": attrs(kinds=BREACH, s3=True),
        "s-4": attrs(kinds=BREACH, s3=True),
        "s-5": attrs(kinds=EXPLOIT),
        "s-6": attrs(kinds=EXPLOIT),
    }
    plan = build_patterns(js, at)
    assert [(p.region, p.kind) for p in plan.patterns] == [
        ("日本", "breach"),
        ("海外", "breach"),  # S3 を含み重い
        ("海外", "exploitation"),
    ]
    assert [p.pattern_id for p in plan.patterns] == ["P1", "P2", "P3"]


def test_japan_related_flag_makes_region_japan() -> None:
    js = [j("s-1", "a。", japan=True), j("s-2", "b。", japan=True)]
    at = {"s-1": attrs(kinds=BREACH), "s-2": attrs(kinds=BREACH)}
    assert build_patterns(js, at).patterns[0].region == "日本"


def test_no_change_judgments_are_standing_not_patterned() -> None:
    js = [j(f"s-{n}", f"継続{n}", delta="no_change") for n in range(6)]
    at = {f"s-{n}": attrs(kinds=BREACH) for n in range(6)}
    plan = build_patterns(js, at)
    assert plan.patterns == () and plan.rest == ()
    assert len(plan.standing) == 4
    assert plan.moved_count == 0
    assert plan.top_judgment is not None


def test_deterministic_regardless_of_input_order() -> None:
    js, at = _jp_breaches()
    assert build_patterns(js, at) == build_patterns(list(reversed(js)), at)


def test_title_is_event_headline_with_most_articles_then_newest() -> None:
    js = [j("s-1", "アルファ証券の一つ目の事案。"), j("s-2", "二つ目の事案。")]
    at = {
        "s-1": attrs(
            kinds=BREACH,
            headlines=(
                EventHeadline("ev-a", 1, "2026-11-17T01:00:00", "アルファ証券の古い事象の見出し"),
                EventHeadline(
                    "ev-b", 3, "2026-11-17T02:00:00", "アルファ証券の記事の多い事象の見出し"
                ),
            ),
        ),
        "s-2": attrs(kinds=BREACH),
    }
    cases = build_patterns(js, at).patterns[0].cases
    by_id = {c.judgment_ids[0]: c for c in cases}
    assert (
        by_id["s-1"].title == "アルファ証券の記事の多い事象の見出し"
        and by_id["s-1"].title_from_event
    )
    assert by_id["s-2"].title == "二つ目の事案" and not by_id["s-2"].title_from_event


def test_event_headline_unrelated_to_the_claim_is_not_used_as_title() -> None:
    js = [j("s-1", "アルファ証券で侵害。"), j("s-2", "ガンマ商事で侵害。")]
    other = (EventHeadline("ev-x", 5, "2026-11-17T00:00:00", "デルタ運輸の別件の事象"),)
    at = {"s-1": attrs(kinds=BREACH, headlines=other), "s-2": attrs(kinds=BREACH)}
    cases = build_patterns(js, at).patterns[0].cases
    first = next(c for c in cases if c.judgment_ids == ("s-1",))
    assert first.title == "アルファ証券で侵害" and not first.title_from_event


def test_title_without_event_is_full_first_sentence_never_truncated() -> None:
    long_claim = "あ" * 200 + "、" + "い" * 50 + "。続き。"
    js = [j("s-1", long_claim), j("s-2", "短い。")]
    at = {k: attrs(kinds=BREACH) for k in ("s-1", "s-2")}
    titles = [c.title for c in build_patterns(js, at).patterns[0].cases]
    assert "あ" * 200 + "、" + "い" * 50 in titles
    assert not any("…" in t for t in titles)


def test_case_weight_uses_s3_large_magnitude_and_stated_count_not_articles() -> None:
    from src.synthesis.grounded.pattern_case import magnitude_bucket

    assert [magnitude_bucket(n) for n in (0, 9_999, 10_000, 99_999, 100_000, 10**9)] == [
        0,
        0,
        1,
        1,
        2,
        4,
    ]
    js = [
        j("s-1", "甲社で顧客 30 万人の情報が漏えいした。"),
        j("s-2", "乙社で侵害が確認された。"),
        j("s-3", "丙社で侵害が確認された。"),
    ]
    at = {
        "s-1": attrs(kinds=BREACH, large=True),
        "s-2": attrs(kinds=BREACH, s3=True),
        "s-3": attrs(kinds=BREACH),
    }
    cases = build_patterns(js, at).patterns[0].cases
    weights = {c.judgment_ids[0]: c.weight for c in cases}
    assert weights == {"s-1": 1 + 2, "s-2": 2, "s-3": 0}  # 30 万 = 桁 2、大規模 1
    assert [c.judgment_ids[0] for c in cases] == ["s-1", "s-2", "s-3"]  # 重い順


def test_large_magnitude_singleton_becomes_notable_without_s3() -> None:
    js = [j("s-1", "甲。"), j("s-2", "乙。"), j("s-3", "丙社で 500 万件の情報が流出した。")]
    at = {"s-1": attrs(kinds=BREACH), "s-2": attrs(kinds=BREACH), "s-3": attrs(large=True)}
    plan = build_patterns(js, at)
    assert [n.case.judgment_ids for n in plan.notable] == [("s-3",)]


def test_single_headline_subject_is_heaviest_case_overall() -> None:
    js = [j("s-1", "甲社で侵害。"), j("s-2", "乙社で侵害。"), j("s-3", "丙社の重大な単独事案。")]
    at = {
        "s-1": attrs(kinds=BREACH),
        "s-2": attrs(kinds=BREACH, s3=True),
        "s-3": attrs(s3=True, large=True),
    }
    plan = build_patterns(js, at)
    assert [c.judgment_ids for c in plan.headline_subjects] == [("s-3",)]  # 重さ 3 > 2
    assert plan.headline_extra is None  # S3 の注目は主語自身だけ
    assert [c.case_id for c in plan.all_cases()] == ["C1", "C2", "C3"]
    at["s-3"] = attrs(s3=True)
    assert build_patterns(js, at).headline_subjects[0].judgment_ids == (
        "s-2",
    )  # 同点は salience 順


def test_links_need_shared_entities_sorted_by_count_and_capped() -> None:
    js = [j(f"s-{n}", f"事案{n}。") for n in range(5)]
    links = {
        "s-0": frozenset({"victim_org:ベータ運輸", "cve:CVE-2026-1"}),
        "s-1": frozenset({"victim_org:ベータ運輸", "cve:CVE-2026-1"}),
        "s-2": frozenset({"cve:CVE-2026-1"}),
        "s-3": frozenset({"cve:CVE-2026-1"}),
        "s-4": frozenset({"malware_family:X"}),
    }
    at = {k: attrs(kinds=BREACH, links=v) for k, v in links.items()}
    plan = build_patterns(js, at)
    assert plan.links[0].shared == ("CVE-2026-1", "ベータ運輸") and len(plan.links) == 3
    assert all("X" not in link.shared for link in plan.links)
    assert build_patterns(js, {k: attrs(kinds=BREACH) for k in links}).links == ()


def test_standing_items_are_all_listed_with_title_and_last_moved_jst() -> None:
    js = [j(f"s-{n}", f"継続{n}の評価。", delta="no_change") for n in range(6)]
    at = {"s-0": attrs(last="2026-11-15T16:00:00+00:00")}  # JST で翌日
    plan = build_patterns(js, at)
    assert len(plan.standing_items) == 6 and len(plan.standing) == 4
    first = next(i for i in plan.standing_items if i.judgment_id == "s-0")
    assert (first.title, first.last_moved) == ("継続0の評価", "2026-11-16")


def test_common_marks_need_two_cases_and_use_catalog_names() -> None:
    js = [j("s-1", "甲。"), j("s-2", "乙。"), j("s-3", "丙。")]
    at = {
        "s-1": attrs(
            kinds=BREACH, techniques=frozenset({"T1566", "T1190"}), products=frozenset({"VPN-X"})
        ),
        "s-2": attrs(kinds=BREACH, techniques=frozenset({"T1566"}), products=frozenset({"VPN-X"})),
        "s-3": attrs(kinds=BREACH, techniques=frozenset({"T1059"})),
    }
    marks = build_patterns(js, at, {"T1566": "Phishing"}).patterns[0].common_marks
    assert [(m.kind, m.label, m.cases) for m in marks] == [
        ("product", "VPN-X", 2),
        ("technique", "T1566 Phishing", 2),
    ]
    assert build_patterns(js, at).patterns[0].common_marks[1].label == "T1566"


def test_s3_singleton_becomes_notable_and_leaves_rest() -> None:
    js = [
        j("s-1", "事案一。"),
        j("s-2", "事案二。"),
        j("s-3", "重大な単独事案。"),
        j("s-4", "軽い動き。"),
    ]
    at = {
        "s-1": attrs(kinds=BREACH),
        "s-2": attrs(kinds=BREACH),
        "s-3": attrs(kinds=EXPLOIT, s3=True),
        "s-4": attrs(),
    }
    plan = build_patterns(js, at)
    assert [(n.item_id, n.case.judgment_ids) for n in plan.notable] == [("S1", ("s-3",))]
    assert [c.judgment_ids for c in plan.rest] == [("s-4",)]


def test_notable_is_capped_at_two() -> None:
    js = [j(f"s-{n}", f"単独{n}。") for n in range(4)]
    at = {f"s-{n}": attrs(s3=True) for n in range(4)}
    plan = build_patterns(js, at)
    assert [n.item_id for n in plan.notable] == ["S1", "S2"]
    assert len(plan.rest) == 2


def test_links_drop_products_generic_words_and_short_names() -> None:
    js = [j("s-1", "事案一。"), j("s-2", "事案二。")]
    shared = frozenset(
        {
            "affected_product:VPN-Alpha",  # 製品は使わない
            "victim_org:ab",  # 短すぎる
            "actor:不正アクセス",  # 一般語
            "campaign:ShadowOps",
        }
    )
    at = {k: attrs(kinds=BREACH, links=shared) for k in ("s-1", "s-2")}
    links = build_patterns(js, at).links
    assert [link.shared for link in links] == [("ShadowOps",)]
