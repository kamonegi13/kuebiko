"""バースト顕在性は PIR 背骨の例外にしない (第 3 回独立レビュー #4、既定=認めない)。

日次バーストは「言及量の急増」= **収集網の観測**であり、重要性の定義ではない。
重要性の背骨は PIR → importance → channel (CLAUDE.md §13 設計原則 2)。バーストを
配信・重要度・記事選抜に効かせると、**収集量による優先度付け**が背骨を上書きする
(第 3 回独立レビューが U 型注入への批判として指摘した構造そのもの)。

指示では止まらないので、**import 境界の関門**で固定する: burst は朝ブリーフの
本文組み立てからのみ参照してよく、triage / routing / PIR 評価から参照してはならない。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC = Path("src")
# バーストを参照してよいのは「表示のために本文へ差し込む」経路だけ
_ALLOWED = {
    Path("src/pipeline/runners.py"),  # 朝ブリーフ本文の組み立て
    Path("src/forecast/burst.py"),  # 実装本体
}
# 背骨側 (ここが burst を読んだら優先度付けが収集量に侵食される)
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


def _burst_importers() -> list[Path]:
    hits: list[Path] = []
    for py in _SRC.rglob("*.py"):
        if py in _ALLOWED:
            continue
        if any(m.startswith("src.forecast.burst") for m in _imports(py)):
            hits.append(py)
    return hits


def test_burst_is_not_imported_outside_the_display_path() -> None:
    # Act
    importers = _burst_importers()

    # Assert — 増えたら「顕在性を背骨へ持ち込んだ」ということ
    assert importers == [], (
        "burst を表示経路の外から参照している: "
        f"{[str(p) for p in importers]} — 収集量による優先度付けは PIR 背骨と衝突する"
    )


@pytest.mark.parametrize("target", [*_BACKBONE_DIRS, *_BACKBONE_FILES])
def test_backbone_does_not_depend_on_burst(target: str) -> None:
    # Arrange
    root = Path(target)
    files = list(root.rglob("*.py")) if root.is_dir() else [root]

    # Act / Assert
    for py in files:
        assert not any(m.startswith("src.forecast.burst") for m in _imports(py)), (
            f"{py} が burst に依存している — 重要性の背骨は PIR → importance → channel"
        )
