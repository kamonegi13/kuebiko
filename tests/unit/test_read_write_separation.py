"""閲覧面に定義の変更を残さない (CLAUDE.md §11、2026-09-16 改訂)。

配置基準:「**専用ページは閲覧のみ、定義の変更はすべて設定カテゴリ**」。
移設した画面に変更系が**取り残される**と、二重の入口ができて「片方だけ直す」事故の
温床になる (2026-09-16 だけで 3 回踏んだ型)。

⚠ **実行・再生成と承認キューは対象外** — 定義の変更ではない (計画 §2 の B/C 群)。
⚠ **直接操作も対象外** — 編集対象がレンダリングされた画面そのもの (C-2 群、widget 配置)。
"""

from __future__ import annotations

import re
from pathlib import Path

_FRONTEND = Path("frontend/src")

#: 閲覧面に残してはいけない「定義の変更」API 呼び出し → 移設先。
_DEFINITION_WRITES = {
    "pirApi.save": "設定 > SIR",
    "pirApi.toggle": "設定 > SIR",
    "pirApi.approve": "設定 > SIR",
    "pirApi.delete": "設定 > SIR",
    "jpciOperatorsApi.save": "設定 > 指定事業者名簿",
}

#: 変更を持ってよい面 (設定カテゴリ配下 + 編集画面そのもの)。
_ALLOWED_PREFIXES = (
    "frontend/src/pages/config/",
    "frontend/src/pages/PirEditPage.tsx",  # 設定配下の編集画面 (/app/config/sir/edit)
)


def _offenders() -> list[str]:
    out: list[str] = []
    for f in (*_FRONTEND.rglob("*.ts"), *_FRONTEND.rglob("*.tsx")):
        path = str(f)
        if "__fixtures__" in path or path.startswith(_ALLOWED_PREFIXES):
            continue
        body = f.read_text(encoding="utf-8")
        for call, dest in _DEFINITION_WRITES.items():
            if call in body:
                out.append(f"{path}: {call} は {dest} にあるべき")
    return out


def test_viewing_pages_do_not_change_definitions() -> None:
    assert _offenders() == [], "閲覧面に定義の変更が残っている:\n" + "\n".join(_offenders())


def test_edit_routes_live_under_config() -> None:
    """定義の編集画面は設定配下の URL で到達する (旧 URL は転送のみ)。"""
    app = (_FRONTEND / "../src/App.tsx").resolve()
    body = app.read_text(encoding="utf-8")

    assert '"/app/config/sir/edit"' in body
    # 旧 URL は **転送のみ** — ルートとして生かさない
    assert re.search(r'window\.location\.replace\(`?/app/config/sir/edit', body)


def test_old_entry_points_redirect_rather_than_duplicate() -> None:
    """旧入口を残さない。二重の入口は「片方だけ直す」事故の温床。"""
    app = (_FRONTEND / "../src/App.tsx").resolve()
    body = app.read_text(encoding="utf-8")

    # 指定事業者名簿の旧ルートは転送に置き換わっている
    assert 'window.location.replace("/app/config#operators")' in body
    assert '{ kind: "jpci-operators" }' not in body
