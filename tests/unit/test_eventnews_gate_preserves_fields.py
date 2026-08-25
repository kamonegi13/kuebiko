"""識別子関門が draft のフィールドを落とさないこと。

``verify_draft`` は解決後の値で ``EventNewsDraft`` / ``FactItem`` を**組み直す**。
組み直しで渡し忘れたフィールドは既定値に落ち、**生成側は正しかったのに保存時点で
失われる**。2026-08-26 実測: ``section`` を渡しておらず本番 542 版の全 fact が
既定の "what" に潰れ、``key_points`` は 542 版すべてでキーごと消えていた。
モデルにフィールドを足すたび同じ事故が起きるので、AST で網羅を固定する。
"""

from __future__ import annotations

import ast
import inspect

from src.eventnews import identifier_gate
from src.eventnews.models import EventNewsDraft, FactItem


def _keywords_passed_to(class_name: str) -> set[str]:
    """``verify_draft`` の中で ``class_name(...)`` に渡しているキーワード名。"""
    tree = ast.parse(inspect.getsource(identifier_gate))
    passed: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "verify_draft":
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            if isinstance(func, ast.Name) and func.id == class_name:
                passed |= {kw.arg for kw in call.keywords if kw.arg}
    return passed


def test_gate_passes_every_draft_field() -> None:
    # Arrange / Act
    passed = _keywords_passed_to("EventNewsDraft")

    # Assert — 既定値のあるフィールドこそ落ちても気付けない
    assert set(EventNewsDraft.model_fields) <= passed


def test_gate_passes_every_fact_field() -> None:
    # Arrange / Act
    passed = _keywords_passed_to("FactItem")

    # Assert
    assert set(FactItem.model_fields) <= passed
