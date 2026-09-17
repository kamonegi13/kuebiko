"""状況総括の対読審判 — 可視欄の抽出、notes 関門、勝者確定 (2026-09-17)。"""

from __future__ import annotations

import json

from scripts.judge_synthesis_pairwise import final_winner, notes_gate, visible_sections


def test_visible_sections_drops_analysis_notes_and_keeps_order() -> None:
    out = json.dumps({"analysis_notes": "内緒", "pir_section": "P", "headline": "H"})
    text = visible_sections(out)
    assert "内緒" not in text
    assert text.index("[headline]") < text.index("[pir_section]")
    assert "[weight_section]\n" in text  # 欠けた欄は空で載る


def test_visible_sections_passes_through_non_json() -> None:
    assert visible_sections("plain") == "plain"


def test_notes_gate_detects_five_aspects() -> None:
    notes = "1. 出典の上流依存 2. 時制監査 3. 前期指標の照合 4. 欠落が判断を阻害 5. 日本への接点"
    gate = notes_gate(json.dumps({"analysis_notes": notes}))
    assert all(gate.values()) and len(gate) == 5
    assert not any(notes_gate(json.dumps({"headline": "x"})).values())
    assert not any(notes_gate("not json").values())


def test_final_winner_requires_agreement_across_order_swap() -> None:
    assert final_winner("M30", "M30") == "M30"
    assert final_winner("M30", "n17c") == "tie"
    assert final_winner("tie", "tie") == "tie"
