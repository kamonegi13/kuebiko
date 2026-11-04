"""scripts/n20_audit の純ロジック (標本・κ・主張の数え方・集計・guard) のテスト。"""

from __future__ import annotations

from typing import Any

import pytest

from scripts.n20_audit import aggregate as ag
from scripts.n20_audit import render
from scripts.n20_audit.common import psql_json
from scripts.n20_audit.guard import Guard
from scripts.n20_audit.run import heavy_to_confirm
from scripts.n20_audit.sampling import allocate, stratified_sample
from scripts.n20_audit.stats import (
    bootstrap_ci,
    cohen_kappa,
    locate_quote,
    per_100,
    reproduction,
    split_claims,
    wilson,
)


def _finding(quote: str, kind: str = "数値・日付の誤り", severity: str = "重い") -> dict[str, Any]:
    return {"quote": quote, "kind": kind, "severity": severity, "material": "なし", "reason": "r"}


# --- sampling ---------------------------------------------------------------


def test_allocate_is_proportional_with_floor_of_one() -> None:
    # Arrange
    sizes = {"a": 80, "b": 15, "c": 5}

    # Act
    alloc = allocate(sizes, 10)

    # Assert
    assert sum(alloc.values()) == 10
    assert alloc["c"] >= 1
    assert alloc["a"] > alloc["b"] >= alloc["c"]


def test_allocate_returns_everything_when_population_is_small() -> None:
    assert allocate({"a": 2, "b": 3}, 30) == {"a": 2, "b": 3}


def test_allocate_never_exceeds_stratum_size() -> None:
    alloc = allocate({"a": 1, "b": 1, "c": 100}, 10)
    assert alloc == {"a": 1, "b": 1, "c": 8}


def test_stratified_sample_is_deterministic_for_a_seed() -> None:
    # Arrange
    items = [{"id": i, "s": "x" if i % 3 else "y"} for i in range(60)]

    # Act
    a = stratified_sample(items, lambda r: str(r["s"]), 12, seed=7)
    b = stratified_sample(items, lambda r: str(r["s"]), 12, seed=7)
    c = stratified_sample(items, lambda r: str(r["s"]), 12, seed=8)

    # Assert
    assert a == b
    assert len(a) == 12
    assert a != c


# --- stats ------------------------------------------------------------------


def test_split_claims_splits_on_sentence_ends_and_newlines() -> None:
    text = "見出し: 攻撃が起きた。\n- 被害は 3 件とされる。原因は不明。\n- ok"
    assert split_claims(text) == ["見出し: 攻撃が起きた。", "被害は 3 件とされる。", "原因は不明。"]


def test_locate_quote_finds_exact_and_rejects_absent() -> None:
    claims = ["攻撃者は CVE-2026-1 を悪用した。", "被害は 300 万件とされる。"]
    assert locate_quote("被害は 300 万件", claims) == (True, 1)
    assert locate_quote("まったく無関係な文字列だ", claims)[0] is False


def test_locate_quote_tolerates_whitespace_and_width() -> None:
    claims = ["被害は３００万件とされる。"]
    assert locate_quote("被害は 300万件", claims) == (True, 0)


def test_cohen_kappa_known_values() -> None:
    assert cohen_kappa([True, False, True, False], [True, False, True, False]) == 1.0
    # 一致 0.5・期待一致 0.5 → κ = 0
    assert cohen_kappa([True, True, False, False], [True, False, True, False]) == 0.0
    # 全件同じラベル → 未定義
    assert cohen_kappa([True, True], [True, True]) is None


def test_cohen_kappa_rejects_length_mismatch() -> None:
    with pytest.raises(ValueError):
        cohen_kappa([True], [True, False])


def test_reproduction_counts_overlap() -> None:
    r = reproduction({("a", 1), ("a", 2)}, {("a", 2), ("b", 0)})
    assert r["n_both"] == 1
    assert r["recall_of_first"] == 0.5
    assert r["jaccard"] == pytest.approx(1 / 3)


def test_per_100_and_wilson() -> None:
    assert per_100(3, 300) == 1.0
    assert per_100(1, 0) is None
    lo, hi = wilson(1, 30) or (0.0, 0.0)
    assert 0.0 < lo < 1 / 30 < hi < 0.2


def test_bootstrap_ci_is_deterministic_and_brackets_point() -> None:
    pairs = [(1.0, 40), (0.0, 30), (2.0, 50), (0.0, 35)]
    a = bootstrap_ci(pairs, seed=1, b=500)
    b = bootstrap_ci(pairs, seed=1, b=500)
    assert a == b
    assert a is not None
    point = 100 * 3 / 155
    assert a[0] <= point <= a[1]


# --- guard ------------------------------------------------------------------


def test_guard_reason_by_id_title_url_and_root() -> None:
    g = Guard(
        ids=frozenset({"rss:a"}),
        titles=frozenset({"評価の見出し"}),
        urls=frozenset({"https://x.example/1"}),
        eval_events=frozenset({"ev-1"}),
        roots_by_article={"rss:b": frozenset({"ev-1"})},
    )
    assert g.reason(article_ids=["rss:a"]) == "id:rss:a"
    assert g.reason(titles=[" 評価の見出し "]) == "title:評価の見出し"
    assert g.reason(urls=["https://x.example/1"]) == "url:https://x.example/1"
    assert g.reason(roots=["ev-1"]) == "root:ev-1"
    assert g.reason(article_ids=["rss:b"]) == "root:ev-1"
    assert g.reason(article_ids=["rss:c"], titles=["別の見出し"]) is None


# --- render -----------------------------------------------------------------


def test_event_news_material_drops_instruction_sections() -> None:
    prompt = "指示の前置き\n## 対象記事 (1 件を提示)\n[1] **見出し** (x)\n本文\n## 出力\n規則"
    expected = "## 対象記事 (1 件を提示)\n[1] **見出し** (x)\n本文"
    assert render.event_news_material(prompt) == expected


def test_event_news_output_renders_sections_with_source_index() -> None:
    body = {"headline": "H", "bluf": "B。", "facts": [{"text": "F。", "source_index": 2}]}
    out = render.event_news_output("fallback", body)
    assert out.splitlines() == ["見出し: H", "BLUF: B。", "事実:", "- [記事 2] F。"]


# --- aggregate --------------------------------------------------------------


def _items(n: int) -> dict[str, dict[str, Any]]:
    claims = ["被害は 300 万件とされる。", "攻撃者は X である。"]
    return {f"i{k}": {"claims": claims} for k in range(n)}


def test_criteria_flags_heavy_and_kinds() -> None:
    c = ag.criteria({"findings": [_finding("q", kind="確度の格上げ", severity="軽い")]})
    assert c["重い指摘あり"] is False
    assert c["指摘あり"] is True
    assert c["種類:確度の格上げ"] is True


def test_self_agreement_perfect_when_passes_match() -> None:
    items = _items(4)
    p: dict[str, dict[str, Any]] = {
        "i0": {"findings": [_finding("被害は 300 万件")]},
        "i1": {"findings": []},
        "i2": {"findings": [_finding("攻撃者は X")]},
        "i3": {"findings": []},
    }
    res = ag.self_agreement(items, p, p)
    assert res["n"] == 4
    assert res["criteria"]["重い指摘あり"]["kappa"] == 1.0
    assert res["finding_reproduction"]["重い"]["recall_of_first"] == 1.0
    assert res["judged_by_rubric"] is True


def test_self_agreement_marks_low_kappa_as_not_judged() -> None:
    items = _items(4)
    hit = [_finding("被害は 300 万件")]
    p1 = {k: {"findings": hit if k in ("i0", "i1") else []} for k in items}
    p2 = {k: {"findings": hit if k in ("i0", "i2") else []} for k in items}
    res = ag.self_agreement(items, p1, p2)
    assert res["criteria"]["重い指摘あり"]["kappa"] == 0.0
    assert res["judged_by_rubric"] is False


def test_baseline_counts_only_confirmed_heavy() -> None:
    items = _items(2)
    pass1 = {
        "i0": {"findings": [_finding("被害は 300 万件"), _finding("攻撃者は X")]},
        "i1": {"findings": [_finding("存在しない引用の文字列", severity="重い")]},
    }
    confirm = {
        "i0": {
            "asked": [
                {"id": "f0", "kind": "数値・日付の誤り"},
                {"id": "f1", "kind": "行為者・対象の誤り"},
            ],
            "items": [
                {"id": "f0", "verdict": "重い違反", "reason": ""},
                {"id": "f1", "verdict": "違反でない", "reason": ""},
            ],
        }
    }
    res = ag.baseline(items, pass1, confirm, seed=1)
    assert res["claims_total"] == 4
    assert res["heavy_confirmed"] == 1
    assert res["heavy_confirmed_per100"] == 25.0
    assert res["heavy_quote_verified_per100"] == 50.0
    assert res["heavy_raw_per100"] == 75.0
    assert res["confirmed_heavy_kinds"] == {"数値・日付の誤り": 1}
    assert res["n20_line_per100"] == 1.0


def test_n20_line_is_min_of_one_and_half_baseline() -> None:
    assert ag.n20_line(1.2) == 0.6
    assert ag.n20_line(5.0) == 1.0
    assert ag.n20_line(None) is None


def test_t4_decision_weights_by_population() -> None:
    rows = [
        {"kind": "translate", "verdict": "問題なし", "boundary": False},
        {"kind": "translate", "verdict": "重い問題あり", "boundary": False, "missing": ["x"]},
        {"kind": "actor_sync", "verdict": "問題なし", "boundary": False},
        {"kind": "translate", "verdict": "重い問題あり", "boundary": True},
    ]
    res = ag.t4_decision(rows, {"translate": 90, "actor_sync": 10})
    assert res["non_boundary"] == 3
    assert res["heavy_non_boundary"] == 1
    assert res["rate_weighted"] == 0.45
    assert res["decision"] == "不合格の型だけ Opus で作り直す"
    assert res["failing_types"] == {"translate:missing": 1}


def test_t4_decision_adopts_when_rate_within_line() -> None:
    rows = [{"kind": "translate", "verdict": "軽微のみ", "boundary": False}] * 20
    assert ag.t4_decision(rows, {"translate": 1})["decision"] == "採用"


def test_translation_agreement_kappa() -> None:
    p1 = {"a": {"verdict": "重い問題あり"}, "b": {"verdict": "問題なし"}}
    p2 = {"a": {"verdict": "重い問題あり"}, "b": {"verdict": "軽微のみ"}}
    res = ag.translation_agreement(p1, p2)
    assert res["重い問題あり"]["kappa"] == 1.0
    assert res["verdict3"]["agreement"] == 0.5


# --- run / common -----------------------------------------------------------


def test_heavy_to_confirm_keeps_only_verified_heavy_quotes() -> None:
    item = {"claims": ["被害は 300 万件とされる。"]}
    audit = {
        "findings": [
            _finding("被害は 300 万件"),
            _finding("被害は 300 万件", severity="軽い"),
            _finding("出力に無い引用文字列です"),
        ]
    }
    got = heavy_to_confirm(item, audit)
    assert [g["id"] for g in got] == ["f0"]
    assert got[0]["claim_idx"] == 0


def test_psql_json_rejects_non_select() -> None:
    with pytest.raises(ValueError):
        psql_json("delete from articles")
