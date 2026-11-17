"""事案どうしのつながりの関門 (id・共通の語・文の関門)。"""

from __future__ import annotations

from src.synthesis.grounded.pattern_relations import RELATIONS_MAX, gate_relations
from src.synthesis.grounded.patterns import PatternPlan, build_patterns
from tests.unit.synthesis_pattern_fixtures import BREACH, attrs, j


def _plan() -> PatternPlan:
    js = [
        j("s-1", "甲社でゼータ製 VPN の脆弱性を突かれ侵害された。"),
        j("s-2", "乙社の拠点が侵害された。"),
        j("s-3", "丙社でも侵害が確認された。"),
        j("s-4", "丁社が侵害された。"),
    ]
    return build_patterns(js, {k: attrs(kinds=BREACH) for k in ("s-1", "s-2", "s-3", "s-4")})


EXC = {"s-2": ("乙社はゼータ製 VPN 経由で侵入された。",)}


def _ids(plan: PatternPlan) -> dict[str, str]:
    return {c.judgment_ids[0]: c.case_id for c in plan.all_cases()}


def test_accepts_when_shared_word_in_both_claims_or_excerpts() -> None:
    plan = _plan()
    ids = _ids(plan)
    pair = [ids["s-1"], ids["s-2"]]
    rels, stats = gate_relations(
        [(pair, "ゼータ製 VPN", "甲社と乙社はゼータ製 VPN を使っていた。")], plan, EXC
    )
    assert [(r.case_ids, r.shared) for r in rels] == [((pair[0], pair[1]), "ゼータ製 VPN")]
    assert stats.accepted == 1 and stats.rejected_shared == 0


def test_rejects_unknown_duplicate_or_same_ids() -> None:
    plan = _plan()
    ids = _ids(plan)
    a, b = ids["s-1"], ids["s-2"]
    text = "甲社と乙社はゼータ製 VPN を使っていた。"
    items = [
        ([a, "C99"], "ゼータ製 VPN", text),
        ([a, a], "ゼータ製 VPN", text),
        ([a], "ゼータ製 VPN", text),
        ([a, b], "ゼータ製 VPN", text),
        ([b, a], "ゼータ製 VPN", text),  # 同じ組の重複
    ]
    rels, stats = gate_relations(items, plan, EXC)
    assert len(rels) == 1 and stats.rejected_ids == 4


def test_rejects_shared_word_missing_from_one_side_generic_or_short() -> None:
    plan = _plan()
    ids = _ids(plan)
    pair = [ids["s-1"], ids["s-3"]]  # s-3 にはゼータの語が無い
    for shared in ("ゼータ製 VPN", "侵害", "VP", ""):
        rels, stats = gate_relations(
            [(pair, shared, f"甲社と丙社は{shared}が共通だった。")], plan, EXC
        )
        assert rels == () and stats.rejected_shared == 1


def test_rejects_text_that_fails_usual_gates() -> None:
    plan = _plan()
    ids = _ids(plan)
    pair = [ids["s-1"], ids["s-2"]]
    for text in (
        "ゼータ製 VPN が共通で、必ず再発する。",  # 禁止語
        "ゼータ製 VPN が共通で、デルタ社も狙われた。",  # 範囲に無い固有名詞
        "甲社と乙社はゼータ製 VPN を使い、委託先経由だった。",  # claim に無い経路の語
        "甲社と乙社は同じ構成だった。",  # shared を含まない
    ):
        rels, stats = gate_relations([(pair, "ゼータ製 VPN", text)], plan, EXC)
        assert rels == () and stats.accepted == 0, text


def test_caps_accepted_relations() -> None:
    plan = _plan()
    ids = _ids(plan)
    exc = dict.fromkeys(("s-1", "s-2", "s-3", "s-4"), ("共通語アルファ。",))
    names = [ids[k] for k in ("s-1", "s-2", "s-3", "s-4")]
    pairs = [[names[0], names[1]], [names[0], names[2]], [names[0], names[3]], [names[1], names[2]]]
    items = [(p, "共通語アルファ", "共通語アルファが両方にある。") for p in pairs]
    rels, _ = gate_relations(items, plan, exc)
    assert len(rels) == RELATIONS_MAX
