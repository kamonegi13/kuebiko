"""再描画は completion を保ち、pair 見積りで本体 / 副を振り直す (2026-09-17)。"""

from __future__ import annotations

from scripts.rerender_sft_teacher_prompts import rerender_rows


def test_rerender_swaps_prompt_and_reassigns_by_pair_budget() -> None:
    rows = [
        {"key": "a", "prompt": "旧" * 5000, "completion": "c" * 1000},
        {"key": "b", "prompt": "旧" * 100, "completion": "c" * 100, "reason": "pair_too_long"},
        {"key": "z", "prompt": "旧", "completion": "c"},
    ]
    prompts = {"a": "新" * 100, "b": "新" * 30000}

    main, side, missing = rerender_rows(rows, prompts, max_pair_tokens=13_000)

    assert [r["key"] for r in main] == ["a"] and main[0]["prompt"] == "新" * 100
    assert main[0]["completion"] == "c" * 1000 and "reason" not in main[0]
    assert [r["key"] for r in side] == ["b"] and side[0]["reason"] == "pair_too_long"
    assert missing == ["z"]
