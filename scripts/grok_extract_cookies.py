#!/usr/bin/env python3
"""普段使いの Chrome から grok.com / x.com の cookie を読み取り、
Playwright の ``storage_state.json`` 形式で書き出す (Phase 2)。

設計意図:
    ``scripts/grok_login.py`` のように Playwright で Chrome を起動して
    ログインさせる方式は、x.ai の Cloudflare ボット管理に弾かれたり、
    実プロファイルを破壊するリスクがあった。本スクリプトは Chrome の
    プロセスに一切触れず、SQLite ファイルを **read-only** で開いて cookie
    を読み取る。

依存:
    - ``browser-cookie3`` (pure Python、Chrome の AES 暗号化 cookie を
      macOS Keychain と連携してデコードする)

使い方:
    # 普段使いの Chrome で grok.com にログイン済みであることが前提
    uv run python scripts/grok_extract_cookies.py

セキュリティ:
    - state.json には auth cookie が入る (data/ は .gitignore で除外済み)
    - macOS のキーチェーンに「Chrome Safe Storage」へのアクセス許可を求める
      ダイアログが出ることがある。許可するとキーチェーン内のキーで
      cookie を復号する
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import browser_cookie3

DEFAULT_STATE_PATH = Path("data/playwright/state.json")
DEFAULT_DOMAINS = ("grok.com", ".grok.com", "x.com", ".x.com", "x.ai", ".x.ai")


def extract_cookies(
    domains: tuple[str, ...] = DEFAULT_DOMAINS,
) -> list[dict[str, Any]]:
    """Chrome から指定ドメインの cookie を読み取り Playwright 形式で返す。"""
    # browser_cookie3.chrome() は CookieJar を返す (キーチェーンを使って復号)
    jar = browser_cookie3.chrome()

    cookies: list[dict[str, Any]] = []
    for cookie in jar:
        # 完全一致 + サブドメインの両方をカバーするため正規化
        host = (cookie.domain or "").lstrip(".").lower()
        if not any(
            host == d.lstrip(".").lower() or host.endswith("." + d.lstrip(".").lower())
            for d in domains
        ):
            continue

        # Playwright の storage_state cookie 形式に変換
        cookies.append(
            {
                "name": cookie.name,
                "value": cookie.value or "",
                "domain": cookie.domain,
                "path": cookie.path or "/",
                "expires": float(cookie.expires) if cookie.expires else -1,
                "httpOnly": bool(cookie._rest.get("HttpOnly", False)),  # noqa: SLF001
                "secure": bool(cookie.secure),
                "sameSite": _normalize_samesite(cookie._rest.get("SameSite")),  # noqa: SLF001
            },
        )
    return cookies


def _normalize_samesite(value: Any) -> str:
    """Chrome の SameSite を Playwright が受け付ける値に変換する。"""
    if not value:
        return "Lax"
    v = str(value).lower()
    if v == "strict":
        return "Strict"
    if v == "none":
        return "None"
    return "Lax"


def write_state(state_path: Path, cookies: list[dict[str, Any]]) -> None:
    """Playwright storage_state.json を書き出す。"""
    state = {"cookies": cookies, "origins": []}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="grok-extract-cookies",
        description="Chrome の cookie から Playwright storage_state.json を生成",
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help=f"出力先 (default: {DEFAULT_STATE_PATH})",
    )
    parser.add_argument(
        "--domain",
        action="append",
        default=None,
        help=(
            "対象ドメイン (複数指定可)。デフォルトは grok.com / x.com / x.ai。"
            " 例: --domain grok.com --domain x.ai"
        ),
    )
    args = parser.parse_args(argv)

    domains = tuple(args.domain) if args.domain else DEFAULT_DOMAINS

    print(f"==> Chrome から cookie を抽出中: domains={list(domains)}", flush=True)
    print(
        "    macOS Keychain のダイアログが出たら 'Allow' を押してください。",
        flush=True,
    )
    try:
        cookies = extract_cookies(domains=domains)
    except browser_cookie3.BrowserCookieError as e:
        print(f"==> エラー: Chrome cookie 読み取りに失敗 ({e})", flush=True)
        print(
            "    ヒント: Chrome を起動した上で 1 度はそのドメインを開いておく必要あり。",
            "    または macOS Keychain で Chrome のアクセスを許可してください。",
            sep="\n",
            flush=True,
        )
        return 1

    if not cookies:
        print(
            "==> 警告: 該当ドメインの cookie が 0 件でした。",
            "    Chrome で対象サイトにログイン済みか確認してください。",
            sep="\n",
            flush=True,
        )
        return 1

    write_state(args.state_path, cookies)
    print(f"==> {len(cookies)} 件の cookie を書き出しました: {args.state_path}", flush=True)
    # 簡易確認: domain ごとの件数
    by_domain: dict[str, int] = {}
    for c in cookies:
        by_domain[c["domain"]] = by_domain.get(c["domain"], 0) + 1
    for d, n in sorted(by_domain.items()):
        print(f"    {d}: {n} cookies", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
