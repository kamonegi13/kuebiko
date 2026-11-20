"""文の区切りの SSoT (2026-11-20)。数える用と切る用の 2 種類を固定する。"""

from __future__ import annotations

from src.pipeline import summary_meta, summary_repetition
from src.spotlight import repetition_guard
from src.tools.sentences import SENTENCE_PATTERN, last_cut_position
from src.tools.text_budget import cut_at_sentence


def test_all_guards_share_one_pattern() -> None:
    for module in (summary_repetition, repetition_guard, summary_meta):
        assert getattr(module, "SENTENCE_PATTERN") is SENTENCE_PATTERN  # noqa: B009


def test_counting_pattern_splits_on_newline_and_marks() -> None:
    parts = [m.group(0).strip() for m in SENTENCE_PATTERN.finditer("一文目。二文目\n三文目!")]

    assert parts == ["一文目。", "二文目", "三文目!"]


def test_cutting_never_splits_a_version_number() -> None:
    text = "影響は版 2.0 以降に及ぶ。修正版 2.1.3 を適用する"

    assert last_cut_position(text) == text.index("。")
    assert cut_at_sentence(text, 20) == "影響は版 2.0 以降に及ぶ。"
