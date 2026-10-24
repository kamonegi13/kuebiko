"""Spotlight を更新の古い PIR から処理する (打ち切りで同じ末尾が滞留しない)。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

from src.pir.models import Pir
from src.spotlight.runner import _load_generated_at, order_by_staleness

_NOW = datetime(2026, 10, 24, tzinfo=UTC)


def _pirs(*ids: str) -> list[Pir]:
    return [cast(Pir, SimpleNamespace(id=i)) for i in ids]


def test_oldest_generated_comes_first() -> None:
    pirs = _pirs("a", "b", "c")
    ts = {"a": _NOW, "b": _NOW - timedelta(days=2), "c": _NOW - timedelta(days=1)}
    assert [p.id for p in order_by_staleness(pirs, ts)] == ["b", "c", "a"]


def test_never_generated_is_first_and_input_is_not_mutated() -> None:
    pirs = _pirs("a", "b")
    out = order_by_staleness(pirs, {"a": _NOW})
    assert [p.id for p in out] == ["b", "a"]
    assert [p.id for p in pirs] == ["a", "b"]


def test_ties_keep_original_order() -> None:
    pirs = _pirs("x", "y", "z")
    assert [p.id for p in order_by_staleness(pirs, {})] == ["x", "y", "z"]


def test_lookup_failure_falls_back_to_empty() -> None:
    class Boom:
        def list_all_latest_spotlights(self, **_: Any) -> list[Any]:
            raise RuntimeError("db down")

    assert _load_generated_at(cast(Any, Boom()), "rolling7") == {}
    assert _load_generated_at(None, "rolling7") == {}


def test_naive_timestamps_are_made_aware() -> None:
    class Repo:
        def list_all_latest_spotlights(self, **_: Any) -> list[Any]:
            return [SimpleNamespace(pir_id="a", generated_at=datetime(2026, 10, 1))]

    assert _load_generated_at(cast(Any, Repo()), "rolling7")["a"].tzinfo is not None
