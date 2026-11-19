"""pipeline が横断パターン版に渡す入力 (旗 off は何も引かない)。"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from src.storage.repo_judgment_attrs import JudgmentAttrs
from src.synthesis.grounded.estimate import Estimate
from src.synthesis.grounded.pipeline import _pattern_inputs
from tests.unit.n20_t5_fixtures import BASE_TS
from tests.unit.synthesis_pattern_fixtures import j


class _Repo:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[list[str], Any, Any]] = []
        self.fail = fail

    def get_judgment_attrs(
        self, ids: list[str], *, since: Any, until: Any
    ) -> dict[str, JudgmentAttrs]:
        self.calls.append((ids, since, until))
        if self.fail:
            raise RuntimeError("db down")
        return {"s-1": JudgmentAttrs()}


def _est() -> Estimate:
    return Estimate("daily", BASE_TS, BASE_TS + timedelta(days=1), (j("s-1", "a。"),))


def test_flag_off_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SYNTHESIS_PATTERNS", raising=False)
    repo = _Repo()
    assert _pattern_inputs(repo, _est(), now=None) == (None, ())  # type: ignore[arg-type]
    assert repo.calls == []


def test_flag_on_reads_attrs_since_period_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNTHESIS_PATTERNS", "shadow")
    monkeypatch.setattr(
        "src.digest.pir_brief.collect_pir_brief", lambda **_: (_ for _ in ()).throw(RuntimeError)
    )
    repo = _Repo()
    attrs, moved = _pattern_inputs(repo, _est(), now=None)  # type: ignore[arg-type]
    assert attrs == {"s-1": JudgmentAttrs()} and moved == ()
    assert repo.calls == [(["s-1"], BASE_TS, BASE_TS + timedelta(days=1))]


def test_attrs_failure_disables_patterns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNTHESIS_PATTERNS", "1")
    assert _pattern_inputs(_Repo(fail=True), _est(), now=None) == (None, ())  # type: ignore[arg-type]
