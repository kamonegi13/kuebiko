"""route handler が event loop を塞がないことの関門。

2026-08-25 の全停止の真因: `async def` の中で **同期の DB 呼び出し** をしている
endpoint が 70 件あった。同期呼び出しは event loop 上で走るため、DB 接続プールの
枯渇などで 30 秒ブロックすると **アプリ全体が無応答**になる (CPU 0% / TCP は
受け付けるが何も返らない / PG 側の接続は 1 本だけ、という形で観測された)。

FastAPI は `def` の handler を threadpool で実行するので、同期処理は `def` で
書けば event loop は空いたままになり、遅くはなっても止まらない。

⭐ この検査は「規約」ではなく**関門**。指示だけでは止まらないことが繰り返し
起きているため、機械的に落とす (docs: deterministic_gates_over_instructions)。
"""

from __future__ import annotations

import ast
import pathlib

# 同期 DB / ストア呼び出しの目印。ここに挙げた語が本文に出るなら blocking とみなす。
_BLOCKING_MARKERS = (
    "repo.",
    "_repo()",
    "load_current_pir_config",
    "._connect(",
    "RunHistoryRepository(",
    "list_articles",
    "store.",
    "load_config",
    "_store",
)
_ROUTE_METHODS = (".get(", ".post(", ".put(", ".delete(", ".patch(")


class _HasAwait(ast.NodeVisitor):
    """この関数自身のスコープに await / async for / async with があるか。"""

    def __init__(self) -> None:
        self.found = False

    def visit_Await(self, node: ast.Await) -> None:
        self.found = True

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.found = True

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self.found = True

    # ネストした関数は別スコープなので辿らない
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return


def _offenders() -> list[str]:
    out: list[str] = []
    for path in sorted(pathlib.Path("src/ui/api").rglob("*.py")):
        src = path.read_text()
        lines = src.splitlines()
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.AsyncFunctionDef) or not node.decorator_list:
                continue
            deco = ast.get_source_segment(src, node.decorator_list[0]) or ""
            if not any(m in deco for m in _ROUTE_METHODS):
                continue
            checker = _HasAwait()
            for stmt in node.body:
                checker.visit(stmt)
            if checker.found:
                continue  # 本物の async handler
            body = "\n".join(lines[node.lineno - 1 : node.end_lineno])
            if any(marker in body for marker in _BLOCKING_MARKERS):
                out.append(f"{path}:{node.lineno} {node.name}")
    return out


def test_no_route_handler_blocks_the_event_loop() -> None:
    """await を持たず DB に触る `async def` handler はゼロであること。

    落ちたら **`async def` を `def` に変えるだけ**。FastAPI が threadpool で
    実行するようになり、遅くはなってもアプリ全体は止まらなくなる。
    """
    offenders = _offenders()
    assert offenders == [], (
        "event loop を塞ぐ handler がある (async def のまま同期 DB 呼び出し):\n  "
        + "\n  ".join(offenders)
    )
