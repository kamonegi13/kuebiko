"""事象の焦点 (docs/eventnews_focus_redesign.md §2・§3) の純粋関数。

錨 (seed の名前) は落とさない / 外れは焦点を動かさない / 一括勧告の CVE は寄与しない /
まとめ記事は一切数えない、が核心。照合は止めるのを「食い違いが明示的なとき」に限る。
"""

from __future__ import annotations

from src.eventnews.identity.focus import (
    DEFAULT_RULE,
    Focus,
    FocusArticle,
    FocusRule,
    compute_focus,
    focus_from_json,
    focus_to_json,
    match_focus,
)


def _art(aid: str, *names: tuple[str, str], kind: str = "", roundup: bool = False) -> FocusArticle:
    return FocusArticle(article_id=aid, names=frozenset(names), kind=kind, is_roundup=roundup)


def _f(*articles: FocusArticle) -> Focus:
    return compute_focus(list(articles))


CVE_A = ("cve", "CVE-2026-0001")
CVE_B = ("cve", "CVE-2026-0002")
ORG_X = ("victim_org", "orgx")
ORG_Y = ("victim_org", "orgy")
APT = ("actor", "apt-one")


class TestComputeFocus:
    def test_single_article_focus_is_its_names_as_anchor(self) -> None:
        focus = _f(_art("a1", CVE_A, ORG_X))

        assert focus.anchor_article == "a1"
        assert focus.name_set == {CVE_A, ORG_X}
        assert {n.origin for n in focus.names} == {"anchor"}
        assert (focus.supporters, focus.outsiders) == (1, 0)

    def test_anchor_is_never_dropped_even_when_majority_moves_on(self) -> None:
        articles = [_art("a1", CVE_A, ORG_X)] + [_art(f"b{i}", ORG_X, CVE_B) for i in range(5)]

        focus = compute_focus(articles)

        origin = {(n.type, n.value): n.origin for n in focus.names}
        assert origin[CVE_A] == "anchor"
        assert origin[CVE_B] == "grown"

    def test_outsider_does_not_move_focus(self) -> None:
        focus = _f(
            _art("a1", CVE_A),
            _art("a2", CVE_A),
            _art("x1", CVE_B, ORG_Y),
            _art("x2", CVE_B, ORG_Y),
            _art("x3", CVE_B, ORG_Y),
        )

        assert focus.name_set == {CVE_A}
        assert (focus.supporters, focus.outsiders) == (2, 3)

    def test_name_below_theta_does_not_grow(self) -> None:
        focus = _f(_art("a1", CVE_A), _art("a2", CVE_A), _art("a3", CVE_A, ORG_X))

        assert ORG_X not in focus.name_set

    def test_min_growers_variant_requires_two_articles(self) -> None:
        articles = [_art("a1", CVE_A), _art("a2", CVE_A, ORG_X)]

        default = compute_focus(articles)
        strict = compute_focus(articles, FocusRule(min_growers=2, version="v-strict"))

        assert ORG_X in default.name_set
        assert ORG_X not in strict.name_set

    def test_roundup_articles_contribute_nothing(self) -> None:
        focus = _f(_art("a1", CVE_A), _art("r1", CVE_B, ORG_Y, roundup=True))

        assert focus.name_set == {CVE_A}
        assert (focus.supporters, focus.outsiders) == (1, 0)

    def test_bulletin_cves_do_not_contribute_but_other_types_do(self) -> None:
        many = tuple(("cve", f"CVE-2026-{i:04d}") for i in range(10, 14))

        focus = _f(_art("a1", ORG_X, *many))

        assert focus.name_set == {ORG_X}

    def test_anchor_falls_back_to_first_article_with_names(self) -> None:
        focus = _f(_art("n0"), _art("a1", CVE_A), _art("a2", CVE_A))

        assert focus.anchor_article == "a1"
        assert focus.name_set == {CVE_A}
        assert focus.outsiders == 1

    def test_empty_when_no_article_has_names(self) -> None:
        focus = _f(_art("a1"), _art("a2"))

        assert focus.is_empty
        assert focus.anchor_article == ""
        assert focus.outsiders == 2

    def test_kind_requires_strict_majority_of_counted_kinds(self) -> None:
        majority = _f(
            _art("a1", CVE_A, kind="advisory"),
            _art("a2", CVE_A, kind="advisory"),
            _art("a3", CVE_A, kind="exploitation"),
            _art("a4", CVE_A, kind="stats"),
            _art("a5", CVE_A, kind="other"),
        )
        tie = _f(_art("a1", CVE_A, kind="advisory"), _art("a2", CVE_A, kind="exploitation"))

        assert majority.kind == "advisory"
        assert tie.kind == ""

    def test_derivative_kind_counts_and_synonyms_are_mapped(self) -> None:
        focus = _f(_art("a1", ORG_X, kind="enforcement"), _art("a2", ORG_X, kind="derivative"))

        assert focus.kind == "derivative"

    def test_replay_is_deterministic_for_same_order(self) -> None:
        articles = [_art("a1", CVE_A, ORG_X), _art("a2", ORG_X), _art("a3", CVE_B)]

        assert compute_focus(articles) == compute_focus(list(articles))

    def test_json_roundtrip(self) -> None:
        focus = _f(_art("a1", CVE_A, kind="advisory"), _art("a2", CVE_A, ORG_X, kind="advisory"))

        assert focus_from_json(focus_to_json(focus)) == focus
        assert DEFAULT_RULE.version


class TestMatchFocus:
    def test_different_cve_is_conflict(self) -> None:
        verdict = match_focus(_f(_art("a", CVE_A)), _f(_art("b", CVE_B)))

        assert verdict.klass == "conflict"
        assert "cve" in verdict.conflicts

    def test_different_victim_is_conflict(self) -> None:
        verdict = match_focus(_f(_art("a", ORG_X, APT)), _f(_art("b", ORG_Y, APT)))

        assert verdict.klass == "conflict"
        assert "victim_org" in verdict.conflicts

    def test_conflict_needs_both_sides_to_name_that_type(self) -> None:
        verdict = match_focus(_f(_art("a", CVE_A, ORG_X)), _f(_art("b", CVE_A)))

        assert verdict.klass == "support"

    def test_advisory_vs_exploitation_on_same_cve_is_lineage(self) -> None:
        verdict = match_focus(
            _f(_art("a", CVE_A, kind="advisory")), _f(_art("b", CVE_A, kind="exploitation"))
        )

        assert verdict.klass == "lineage"

    def test_derivative_against_incident_is_lineage(self) -> None:
        verdict = match_focus(
            _f(_art("a", ORG_X, kind="breach")), _f(_art("b", ORG_X, kind="derivative"))
        )

        assert verdict.klass == "lineage"

    def test_derivative_with_derivative_is_support(self) -> None:
        verdict = match_focus(
            _f(_art("a", ORG_X, kind="derivative")), _f(_art("b", ORG_X, kind="derivative"))
        )

        assert verdict.klass == "support"

    def test_empty_focus_is_weak(self) -> None:
        verdict = match_focus(_f(_art("a")), _f(_art("b", CVE_A)))

        assert (verdict.klass, verdict.reason) == ("weak", "empty")

    def test_actor_name_only_share_is_weak(self) -> None:
        verdict = match_focus(
            _f(_art("a", APT, CVE_A)), _f(_art("b", APT, ("malware_family", "m")))
        )

        # CVE 側は b に無いので食い違いにならず、共有はアクター名だけ
        assert (verdict.klass, verdict.reason) == ("weak", "actor_only")

    def test_no_shared_name_without_contradiction_is_weak(self) -> None:
        verdict = match_focus(_f(_art("a", APT)), _f(_art("b", CVE_A)))

        assert (verdict.klass, verdict.reason) == ("weak", "no_shared")

    def test_shared_cve_and_same_kind_is_support(self) -> None:
        verdict = match_focus(
            _f(_art("a", CVE_A, kind="exploitation")), _f(_art("b", CVE_A, kind="exploitation"))
        )

        assert verdict.klass == "support"
        assert verdict.shared
