"""構成の軸を決定論で測る (2026-09-20、SYNTHESIS §58)。

⚠ 「接地が退行の軸」という §53 以来の説明は、独立採点で崩れた (12/12 → 5/11 = 偶然水準)。
代わりに Opus の自由記述が挙げたのは**構成**だった:
- 節 (what/scope/how/...) への振り分けが偏り、一色化する
- **相違欄に同一文が重複し、しかも自己矛盾する** (「数値自体は一致するが」と書きながら相違として挙げる)
- 手口の不在を明示する節を設けるかどうか

このうち上 2 つは機械で数えられる。⭐ **審判に依存しない指標にできるものは、指標にする**。
"""

from __future__ import annotations

import pytest

from src.eventnews.structure_metrics import (
    duplicate_lines,
    section_concentration,
    self_contradicting_discrepancies,
)


class _F:
    def __init__(self, text: str, section: str = "what") -> None:
        self.text = text
        self.section = section


# ---------- 節の偏り ----------


def test_all_lines_in_one_section_is_maximum_concentration() -> None:
    facts = [_F("a"), _F("b"), _F("c")]

    assert section_concentration(facts) == pytest.approx(1.0)


def test_evenly_spread_sections_score_low() -> None:
    facts = [_F("a", "what"), _F("b", "scope"), _F("c", "how"), _F("d", "response")]

    assert section_concentration(facts) == pytest.approx(0.25)


def test_no_lines_is_not_concentrated() -> None:
    assert section_concentration([]) == 0.0


# ---------- 重複 ----------


def test_identical_lines_are_counted_once_as_a_duplicate() -> None:
    items = [_F("同じ文である。"), _F("同じ文である。"), _F("違う文。")]

    assert duplicate_lines(items) == 1


def test_near_identical_lines_count_as_duplicates() -> None:
    """空白や句点の違いで重複を見逃さない。"""
    items = [_F("A 社が公表した"), _F("A社が公表した。")]

    assert duplicate_lines(items) == 1


def test_distinct_lines_are_not_duplicates() -> None:
    assert duplicate_lines([_F("A が起きた"), _F("B が起きた")]) == 0


# ---------- 自己矛盾した相違 ----------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("一方は 5 件、もう一方は 5 件と報じており、数値自体は一致するが表現が異なる。", True),
        ("記事 [1] は 5 件、記事 [2] は 7 件と食い違う。", False),
        ("両者とも同じ値を報じている。", True),
        ("侵入経路について記事間で説明が異なる。", False),
    ],
)
def test_detects_contradictions_that_admit_agreement(text: str, expected: bool) -> None:
    """「相違」と称しながら一致を認めている行を拾う。"""
    got = self_contradicting_discrepancies([_F(text)])

    assert (got == 1) is expected
