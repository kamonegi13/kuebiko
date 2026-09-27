"""CTIBench 評価の採点部品 (scripts/eval_ctibench.py、2026-09-26)。"""

from __future__ import annotations

import pytest

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


async def test_ask_all_keeps_input_order_and_caps_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """並列化 (2026-09-27) しても応答は入力と同じ順。同時実行は上限を超えない。"""
    import asyncio

    import scripts.eval_ctibench as m

    running = 0
    peak = 0

    async def fake_ask(client: object, prompt: str, max_tokens: int) -> str:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01 * (5 - int(prompt)))  # 後の問ほど早く終わる
        running -= 1
        return f"answer {prompt}"

    monkeypatch.setattr(m, "_ask", fake_ask)

    got = await m._ask_all(
        None,  # type: ignore[arg-type]  # _ask を差し替えているので使われない
        ["1", "2", "3", "4"],
        10,
        concurrency=2,
        label="t",
    )

    assert got == ["answer 1", "answer 2", "answer 3", "answer 4"]
    assert peak == 2
