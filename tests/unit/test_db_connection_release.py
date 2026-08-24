"""DB 接続が必ずプールへ返ることの関門。

2026-08-25 の全停止の真因: `GET /api/v1/feed-options` が
``conn = connect()`` で接続を取り、**close も with も無かった**。呼ばれるたびに
プール接続が 1 本ずつ永久に失われ、上限に達した時点でアプリ全体が
30 秒 timeout で無応答になった (負荷が引いても自力で戻らない)。

観測された形が分かりにくい点も記録しておく: 漏れた wrapper は GC されて PG 側の
接続は閉じるため、**PG から見ると接続はほぼ 0 本**なのに、プールは「貸出中」と
数えたままになる。「DB は暇なのにアプリだけ止まる」ように見える。

⭐ 規約ではなく関門にする。取得と解放が離れて書ける以上、レビューでは落ちる。
"""

from __future__ import annotations

import ast
import pathlib

_ACQUIRE_NAMES = ("connect", "backend_connect")


def _acquires(node: ast.AST) -> bool:
    """この式が DB 接続を取得する呼び出しか。"""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
    return name in _ACQUIRE_NAMES


def _released_names(fn: ast.AST, src: str) -> set[str]:
    """with で束縛された名前 + try/finally で close される名前。"""
    out: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.With):
            for item in node.items:
                if _acquires(item.context_expr) and isinstance(item.optional_vars, ast.Name):
                    out.add(item.optional_vars.id)
        if isinstance(node, ast.Try) and node.finalbody:
            body = "\n".join((ast.get_source_segment(src, s) or "") for s in node.finalbody)
            for call in ast.walk(ast.parse(body)) if body.strip() else []:
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "close"
                    and isinstance(call.func.value, ast.Name)
                ):
                    out.add(call.func.value.id)
    return out


def _offenders() -> list[str]:
    out: list[str] = []
    for path in sorted(pathlib.Path("src").rglob("*.py")):
        src = path.read_text()
        tree = ast.parse(src)
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            released = _released_names(fn, src)
            # そのまま呼び手へ返す factory (`return con`) は呼び手側が閉じる契約
            returned = {
                n.value.id
                for n in ast.walk(fn)
                if isinstance(n, ast.Return) and isinstance(n.value, ast.Name)
            }
            for node in ast.walk(fn):
                if not isinstance(node, ast.Assign) or not _acquires(node.value):
                    continue
                target = node.targets[0]
                if not isinstance(target, ast.Name):
                    continue
                if target.id in released or target.id in returned:
                    continue
                out.append(f"{path}:{node.lineno} {fn.name}() の {target.id}")
    return out


def test_every_db_connection_is_released() -> None:
    """取得した接続は with / try-finally / 呼び手への return のいずれかで必ず手放す。

    落ちたら ``with connect() as conn:`` に書き換えること
    (``_PgConnection.__exit__`` が putconn する)。
    """
    offenders = _offenders()
    assert offenders == [], (
        "プールへ返されない DB 接続がある (1 回の呼び出しごとに 1 本失う):\n  "
        + "\n  ".join(offenders)
    )
