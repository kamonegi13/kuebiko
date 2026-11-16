"""Spotlight の同じ文の繰り返しの関門 (SPOTLIGHT_REPETITION_GUARD)。"""

from __future__ import annotations

import pytest

from src.spotlight.repetition_guard import dedupe_repeated_sentences, guard_spotlight_text

_REP = "記事では、この作戦が北朝鮮系であるとの評価は示されていない。"


def _outlook(n: int) -> str:
    return (
        "(a) 今期のパターン: 供給網の悪用が続いた。"
        + _REP * n
        + "\n\n(c) 推奨 (定型の対応表): 検知を確認する。"
    )


def test_removes_later_copies_and_keeps_following_text() -> None:
    out, removed = dedupe_repeated_sentences(_outlook(151))
    assert out.count(_REP) == 1
    assert removed == 150
    assert "(c) 推奨" in out  # 後ろの別の文は残す


def test_two_copies_are_not_touched() -> None:
    text = _outlook(2)
    out, removed = dedupe_repeated_sentences(text)
    assert (out, removed) == (text, 0)


def test_short_sentences_are_not_counted() -> None:
    text = "確度は中。" * 5
    assert dedupe_repeated_sentences(text) == (text, 0)


@pytest.mark.parametrize(("mode", "expect_fixed"), [("1", True), ("shadow", False), ("0", False)])
def test_modes(monkeypatch: pytest.MonkeyPatch, mode: str, expect_fixed: bool) -> None:
    monkeypatch.setenv("SPOTLIGHT_REPETITION_GUARD", mode)
    text = _outlook(5)
    out = guard_spotlight_text(text, field="outlook", pir_id="p")
    assert (out.count(_REP) == 1) is expect_fixed


def test_generator_guard_is_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.spotlight import generator

    monkeypatch.setenv("SPOTLIGHT_REPETITION_GUARD", "1")
    assert generator._guard(_outlook(4), "outlook", "p").count(_REP) == 1  # noqa: SLF001
