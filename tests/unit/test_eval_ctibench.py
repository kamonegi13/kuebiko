"""CTIBench 評価の採点部品 (scripts/eval_ctibench.py、2026-09-26)。"""

from __future__ import annotations

from scripts.eval_ctibench import extract_ids, extract_letter, micro_f1


def test_extract_ids_reads_only_the_final_line_and_folds_subtechniques() -> None:
    text = "T1059 is reasoning here\nfinal:\nT1071, T1573.001, T1083"
    assert extract_ids(text) == {"T1071", "T1573", "T1083"}


def test_extract_letter_takes_last_line_with_a_choice() -> None:
    assert extract_letter("I think A is wrong.\nAnswer:\nB") == "B"
    assert extract_letter("no choice here") is None


def test_micro_f1_counts_across_items() -> None:
    pairs = [({"T1", "T2"}, {"T1"}), (set(), {"T3"})]  # tp 1 / fp 1 / fn 1
    m = micro_f1(pairs)
    assert m["precision"] == 0.5 and m["recall"] == 0.5 and m["f1"] == 0.5
    assert micro_f1([])["f1"] == 0.0
