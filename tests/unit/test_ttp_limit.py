"""TTP の件数の歯止め (2026-10-24) — 引用の使い回し・存在しない ID・上限。

引用つきの関門 (filter_by_quotes) は「引用が本文に在るか」しか見ないので、モデルが同じ一節を
30 個の技術に使い回しても通る (s24c の TTP 過多 21 件)。使い回しと実在しない ID を落とし、
残りを上限で切る。上限では語の裏付けのある技術を先に残す。
"""

from __future__ import annotations

import re

import pytest

from src.cti.ttp_evidence import LIMIT_ENV, limit_mode, limit_techniques

_CATALOG = frozenset({f"T1589.{i:03d}" for i in range(1, 15)} | {"T1190", "T1078", "T1566.001"})
_NAMES = {"T1190": "Exploit Public-Facing Application"}
_PATS = {"T1190": (re.compile(r"actively\s+exploit", re.IGNORECASE),)}


def _limit(
    techniques: list[str],
    evidence: list[tuple[str, str]],
    text: str = "body",
    *,
    max_items: int = 12,
    mode: str = "on",
) -> list[str]:
    return limit_techniques(
        techniques,
        evidence,
        text,
        max_items=max_items,
        mode=mode,
        catalog=_CATALOG,
        patterns=_PATS,
        names=_NAMES,
    )


def test_unknown_technique_ids_are_dropped() -> None:
    result = _limit(["T1190", "T1589.020"], [("T1190", "q one"), ("T1589.020", "q two")])

    assert result == ["T1190"]


def test_quote_reused_beyond_two_techniques_is_dropped() -> None:
    techs = ["T1589.001", "T1589.002", "T1589.003", "T1589.004"]
    evidence = [(t, "the actor exfiltrated mailboxes") for t in techs]

    assert _limit(techs, evidence) == ["T1589.001", "T1589.002"]


def test_reuse_is_judged_after_whitespace_and_case_normalization() -> None:
    techs = ["T1589.001", "T1589.002", "T1589.003"]
    evidence = [
        ("T1589.001", "The  Actor"),
        ("T1589.002", "the actor"),
        ("T1589.003", "THE ACTOR"),
    ]

    assert _limit(techs, evidence) == ["T1589.001", "T1589.002"]


def test_cap_keeps_lexically_supported_techniques_first() -> None:
    techs = [f"T1589.{i:03d}" for i in range(1, 11)] + ["T1190"]
    evidence = [(t, f"quote {t}") for t in techs]

    result = _limit(techs, evidence, "attackers actively exploit it", max_items=5)

    assert len(result) == 5
    assert result[0] == "T1190"


def test_under_cap_is_unchanged_and_keeps_order() -> None:
    techs = ["T1078", "T1190", "T1566.001"]
    evidence = [(t, f"quote {t}") for t in techs]

    assert _limit(techs, evidence) == techs


def test_techniques_without_evidence_are_not_treated_as_reuse() -> None:
    # 引用の欄が無い (評価外の出力形式) ときは使い回しの判定をしない
    techs = ["T1589.001", "T1589.002", "T1589.003"]

    assert _limit(techs, []) == techs


def test_non_technique_values_pass_through() -> None:
    assert _limit(["G0016", "T1190"], [("T1190", "q")]) == ["G0016", "T1190"]


def test_shadow_mode_returns_input_unchanged() -> None:
    techs = ["T1589.001", "T1589.020"]

    assert _limit(techs, [("T1589.001", "q"), ("T1589.020", "q")], mode="shadow") == techs


def test_input_list_is_not_mutated() -> None:
    techs = ["T1190", "T1589.020"]
    snapshot = list(techs)

    _limit(techs, [])

    assert techs == snapshot


class TestLimitMode:
    def test_default_is_shadow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(LIMIT_ENV, raising=False)

        assert limit_mode() == "shadow"

    def test_known_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(LIMIT_ENV, "on")

        assert limit_mode() == "on"
