"""遡及生成の ``generated_at`` は 1 件ごとの時刻であること。

2026-08-25 の遡及では 2 時間 32 分かけて生成した 253 件が **全部同じ generated_at**
になっていた (ループの外で 1 回だけ ``datetime.now()`` を取っていた)。版履歴に
しか使っていないので実害は小さいが、このリポジトリで繰り返し踏んでいる
「DB へ書いた時刻と事象の時刻を混同する」型なので関門で止める。
"""

from __future__ import annotations

import ast
import inspect
import pathlib

from src.eventnews import runner


def test_generated_at_is_taken_per_item() -> None:
    """``datetime.now`` の呼び出しが **ループの中** にあること。"""
    tree = ast.parse(inspect.getsource(runner.generate_pending))
    func = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef))
    loops = [n for n in ast.walk(func) if isinstance(n, ast.For)]
    assert loops, "生成ループが見つからない"

    def _now_calls(node: ast.AST) -> int:
        return sum(
            1
            for n in ast.walk(node)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "now"
        )

    in_loop = sum(_now_calls(loop) for loop in loops)
    total = _now_calls(func)
    assert in_loop >= 1, "ループ内で時刻を取っていない (全件が同じ generated_at になる)"
    assert total == in_loop, "ループ外でも時刻を取っている (どちらが使われるか曖昧)"


def test_module_documents_why() -> None:
    """将来ループ外へ戻されないよう、理由をコードに残す。"""
    src = pathlib.Path(runner.__file__).read_text()
    assert "1 件ごとに時刻を取る" in src
