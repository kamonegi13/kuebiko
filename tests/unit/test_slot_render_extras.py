"""SLOT_RENDER の補助 (言い換えの除去・SIR 節の圧縮・連鎖節)。旗 off は従来と同一。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.synthesis.grounded import slot_render
from src.synthesis.grounded.estimate import KeyJudgment
from src.synthesis.grounded.render import build_render_plan
from src.synthesis.grounded.slot_render import ClauseItem, WireJudgmentClauses
from src.synthesis.grounded.slot_render_extras import (
    compact_pir_section,
    first_sentence,
    grouped_chain_text,
    is_near_duplicate,
)
from tests.unit.n20_t5_fixtures import estimate, judgment

PARAPHRASES = [
    ("製造業を狙う侵入が拡大している", "製造業を標的とした侵入の拡大が続いている"),
    (
        "国家支援とみられる組織がVPN機器の脆弱性を悪用し、製造業の企業へ侵入を続けている",
        "VPN機器の脆弱性を悪用した製造業への侵入が続いている",
    ),
]
DIFFERENT = [
    (
        "製造業を狙う侵入が拡大している",
        "同種の侵入の再発に備え、VPN機器のパッチ適用を優先する必要がある",
    ),
    ("製造業を狙う侵入が拡大している", "国内の部品供給網に停止リスクが波及する恐れがある"),
    ("ランサムウェア攻撃で病院の業務が停止した", "同様の攻撃に備えバックアップの隔離を確認すべき"),
]


@pytest.mark.parametrize(("a", "b"), PARAPHRASES)
def test_paraphrase_is_duplicate(a: str, b: str) -> None:
    assert is_near_duplicate(a, b)


@pytest.mark.parametrize(("a", "b"), DIFFERENT)
def test_different_implication_is_kept(a: str, b: str) -> None:
    assert not is_near_duplicate(a, b)


def test_empty_is_not_duplicate() -> None:
    assert not is_near_duplicate("", "abc")


def test_first_sentence_cuts() -> None:
    assert first_sentence("一文目。二文目。") == "一文目"
    long = "あ" * 50 + "、" + "い" * 100 + "。"
    assert first_sentence(long) == "あ" * 50 + "…"
    assert first_sentence("い" * 130) == "い" * 120 + "…"


def test_dedup_flag_drops_duplicate_implication(monkeypatch: pytest.MonkeyPatch) -> None:
    j = judgment("s-x")
    world, imp = PARAPHRASES[0]
    off = slot_render.render_judgment(j, world, imp, with_delta=False)
    monkeypatch.setenv("SLOT_RENDER_DEDUP", "1")
    on = slot_render.render_judgment(j, world, imp, with_delta=False)
    assert off.endswith(imp + "。")
    assert imp not in on and "可能性" in on  # 確度の文は残る
    kept = slot_render.render_judgment(j, world, DIFFERENT[0][1], with_delta=False)
    assert kept.endswith(DIFFERENT[0][1] + "。")


def test_dedup_flag_headline(monkeypatch: pytest.MonkeyPatch) -> None:
    head = replace(estimate().judgments[0], implication="製造業を標的とした侵入の拡大が続いている")
    clause = "製造業を狙う侵入が拡大している"
    off = slot_render._headline(clause, head, {"banned_replaced": 0})
    monkeypatch.setenv("SLOT_RENDER_DEDUP", "1")
    on = slot_render._headline(clause, head, {"banned_replaced": 0})
    assert head.implication in off and head.implication not in on
    assert "可能性" in on


def _pir_judgments() -> tuple[KeyJudgment, ...]:
    return tuple(
        replace(
            judgment(f"s-{i}", confidence="high"),
            claim=f"主張{i}の一文目、" + "あ" * 200 + "。二文目は載せない。",
            pir_ids=("pir_a",),
        )
        for i in range(5)
    )


def test_compact_pir_limits_and_cuts() -> None:
    text = compact_pir_section(_pir_judgments(), {"pir_a": "SIR-A"}, empty="なし")
    assert text.count("(高確度)") == 3 and "ほか 2 件" in text
    assert "二文目" not in text and len(text) < 600


def test_compact_pir_empty() -> None:
    assert compact_pir_section((), {}, empty="なし") == "なし"


def test_grouped_chain_counts_only() -> None:
    text = grouped_chain_text(_pir_judgments(), {"pir_a": "SIR-A"})
    assert "SIR-A」に紐づく判定: 5 件" in text
    assert "同じ記事を根拠に持つ判定の組: 10 組" in text  # fixture は全判定が a1 を引用
    assert grouped_chain_text((), {}) == ""


def test_assemble_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    est = estimate()
    est = replace(est, judgments=tuple(replace(j, pir_ids=("pir_a",)) for j in est.judgments))
    plan = build_render_plan(est=est, period_label="p", template=slot_render.TEMPLATE_V9)
    clauses = WireJudgmentClauses(
        headline_clause="h",
        items=[ClauseItem(judgment_id="s-aaa", world_clause="w", implication_clause="i")],
    )
    base, _ = slot_render.assemble_sections(clauses, est=est, plan=plan)
    assert base.chain_section in (slot_render.NO_RELATIONS, slot_render.RELATIONS_NOT_USED)
    monkeypatch.setenv("SLOT_CHAIN_GROUPING", "1")
    monkeypatch.setenv("SLOT_PIR_COMPACT", "1")
    on, _ = slot_render.assemble_sections(clauses, est=est, plan=plan)
    assert "に紐づく判定: 3 件" in on.chain_section
    assert on.pir_section.startswith("- ")
