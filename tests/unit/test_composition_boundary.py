"""構成比測定は「収集網の観測」であって重要性ではない (burst と同じ関門)。

`src.assessment.composition` は**コホート上の記事構成**を測る。これは収集網が何を
拾ったかの観測であり、重要性の定義ではない。重要性の背骨は PIR → importance → channel
(CLAUDE.md §13 設計原則 2)。構成比を配信・重要度・記事選抜へ効かせると、burst で
指摘されたのと同じ構造 — **収集量による優先度付けが背骨を上書きする** — が再発する。

指示では止まらないので import 境界で固定する (tests/unit/test_burst_boundary.py と同型)。
構成比を読んでよいのは **問い (常設情報要求) の評価と、その表示**だけである。
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path("src")
_MODULE = "src.assessment.composition"

#: 構成比を読んでよい経路 — 問いの評価とその表示のみ。
_ALLOWED = {
    Path("src/assessment/composition.py"),  # 実装本体
    Path("src/assessment/aggregate_signal.py"),  # ACH へ供給する整形層 (段B-3d)
}

#: 背骨側 — ここが構成比を読んだら優先度付けが収集量に侵食される。
_BACKBONE_DIRS = ("src/pir", "src/tools", "src/cti", "src/taxonomy")
_BACKBONE_FILES = (
    "src/pipeline/filters.py",
    "src/pipeline/orchestrator.py",
    "src/pipeline/dispatch.py",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def _importers() -> list[Path]:
    return [
        py
        for py in _SRC.rglob("*.py")
        if py not in _ALLOWED and any(m.startswith(_MODULE) for m in _imports(py))
    ]


def test_backbone_does_not_import_composition() -> None:
    """triage / routing / PIR 評価から構成比を読まない。"""
    offenders = [
        p for p in _importers() if str(p).startswith(_BACKBONE_DIRS) or str(p) in _BACKBONE_FILES
    ]

    assert offenders == [], f"背骨が構成比を読んでいる: {offenders}"


def test_composition_does_not_import_importance_or_routing() -> None:
    """逆向きも塞ぐ — 構成比が重要度・配信の語彙を知らないこと。

    知らなければ、後から「ついでに importance を上げる」という書き足しができない。
    """
    modules = _imports(Path("src/assessment/composition.py"))

    forbidden = [m for m in modules if m.startswith(("src.cti.router", "src.pir", "src.tools"))]

    assert forbidden == [], f"構成比が背骨を読んでいる: {forbidden}"


def test_composition_module_is_dependency_free() -> None:
    """測定核は純粋 — DB も LLM も読まない (テスト可能性と、誤用の抑止)。

    SQL を持たせると「本番データで動くから正しい」と錯覚しやすく、窓の切り方の誤りが
    検算されないまま通る。行を渡す形にして、切り方の責任を呼出側に置く。
    """
    modules = _imports(Path("src/assessment/composition.py"))

    assert not [m for m in modules if m.startswith(("src.storage", "src.tools.llm"))]
