"""relation の学習・測定の窓 (RELATION_TRAIN_WINDOW_DAYS)。推論の窓は変えない。"""

from __future__ import annotations

import pytest

pytest.importorskip("sklearn")

import importlib  # noqa: E402
from typing import Any  # noqa: E402

from src.eventnews.relations import WINDOW_DAYS  # noqa: E402

# scripts を静的 import すると mypy が scripts 側の型を辿る — 動的に読む
rel: Any = importlib.import_module("scripts.train_relation_model")


def test_default_is_inference_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RELATION_TRAIN_WINDOW_DAYS", raising=False)
    assert rel.train_window_days() == WINDOW_DAYS


@pytest.mark.parametrize(
    ("raw", "expected"), [("120", 120), ("0", WINDOW_DAYS), ("x", WINDOW_DAYS)]
)
def test_env_override(monkeypatch: pytest.MonkeyPatch, raw: str, expected: int) -> None:
    monkeypatch.setenv("RELATION_TRAIN_WINDOW_DAYS", raw)
    assert rel.train_window_days() == expected


def test_export_keeps_inference_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RELATION_TRAIN_WINDOW_DAYS", "120")

    class _M:
        estimators_: list[object] = []

    out = rel.export(_M(), 0.5, 10)
    assert out["window_days"] == WINDOW_DAYS
    assert out["train_window_days"] == 120
