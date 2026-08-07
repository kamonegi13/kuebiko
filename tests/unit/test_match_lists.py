"""語彙拡張② match_lists のテスト (純ロジック: マッチ + 検証 + 正規化)。"""

from __future__ import annotations

from src.cti.match_lists import MatchList, match_lists_for_text, validate_match_lists


def test_match_substring_case_insensitive() -> None:
    lists = [
        MatchList(name="vendors", terms=("Fortinet", "FortiOS")),
        MatchList(name="cloud", terms=("AWS",)),
    ]
    assert match_lists_for_text("A fortinet FORTIOS advisory", lists=lists) == frozenset(
        {"vendors"}
    )
    assert match_lists_for_text("AWS outage today", lists=lists) == frozenset({"cloud"})
    assert match_lists_for_text("nothing relevant", lists=lists) == frozenset()


def test_match_empty_pool() -> None:
    assert match_lists_for_text("anything", lists=[]) == frozenset()


def test_terms_coercion_strips_and_drops_empty() -> None:
    ml = MatchList(name="a", terms=["x", " y ", "", "z"])  # type: ignore[arg-type]
    assert ml.terms == ("x", "y", "z")


def test_validate_ok() -> None:
    assert validate_match_lists([{"name": "vendors", "terms": ["Fortinet"]}]) == []


def test_validate_rejects_empty_name_dupes_empty_terms_bad_chars() -> None:
    assert any("必須" in e for e in validate_match_lists([{"name": "", "terms": ["x"]}]))
    dup = validate_match_lists([{"name": "a", "terms": ["x"]}, {"name": "a", "terms": ["y"]}])
    assert any("重複" in e for e in dup)
    assert any("terms" in e for e in validate_match_lists([{"name": "a", "terms": []}]))
    assert any("英数字" in e for e in validate_match_lists([{"name": "bad name!", "terms": ["x"]}]))
