"""SUBJECT_KIND_GATE (SUBJECT-KIND-SHADOW) の旗の 3 値と種別ごとの当たり。"""

from __future__ import annotations

from typing import Any

import pytest

from src.cti import subject_kind_gate as g


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "shadow"),
        ("0", "0"),
        ("off", "0"),
        ("shadow", "shadow"),
        ("1", "1"),
        ("zzz", "shadow"),
    ],
)
def test_gate_mode_parses_flag(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: str
) -> None:
    if raw is None:
        monkeypatch.delenv(g.GATE_ENV, raising=False)
    else:
        monkeypatch.setenv(g.GATE_ENV, raw)
    assert g.gate_mode() == expected


@pytest.mark.parametrize(
    ("kind", "hit"),
    [("advisory", True), ("stats", True), ("roundup", True), ("breach", False), ("other", False)],
)
def test_is_hit_by_kind(kind: str, hit: bool) -> None:
    assert g.is_hit(kind, {"apt29"}) is hit


def test_is_hit_false_without_subjects() -> None:
    assert g.is_hit("advisory", set()) is False


@pytest.mark.parametrize(("mode", "kept"), [("0", True), ("shadow", True), ("1", False)])
def test_effective_subjects_only_drops_in_on_mode(mode: str, kept: bool) -> None:
    out = g.effective_subjects("advisory", {"apt29"}, mode=mode)
    assert (out == {"apt29"}) is kept


def test_effective_subjects_keeps_actor_kinds_in_on_mode() -> None:
    assert g.effective_subjects("breach", {"apt29"}, mode="1") == {"apt29"}


class _Conn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def __enter__(self) -> _Conn:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def execute(self, *_a: object) -> _Conn:
        return self

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _Repo:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def _connect(self) -> _Conn:
        return _Conn(self._rows)


def _row(aid: str, ids: str) -> dict[str, Any]:
    return {
        "article_id": aid,
        "subject_actor_ids": ids,
        "subject_actor_source": "feed",
        "subject_actor_confidence": "high",
    }


def test_record_hits_counts_only_non_actor_kinds_with_subjects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(g.GATE_ENV, "shadow")
    monkeypatch.setattr("src.assessment.situation_track.registry_is_group", lambda: lambda _a: True)
    repo = _Repo([_row("a1", "apt29"), _row("a2", "")])
    kinds = {"a1": "advisory", "a2": "roundup", "a3": "breach"}
    assert g.record_hits(repo, kinds) == 1


def test_record_hits_noop_when_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(g.GATE_ENV, "0")
    assert g.record_hits(_Repo([]), {"a1": "advisory"}) == 0
