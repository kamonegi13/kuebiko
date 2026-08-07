#!/usr/bin/env python3
"""Grok 初回ログイン用 CLI ヘルパ (Phase 2)。

Playwright を **headed モード** で起動し、ユーザが手動で grok.com にログイン
する。ログイン後、セッション state を ``data/playwright/state.json`` に
保存する。以降の自動取得 (GrokFetcher) はこの state を読み込んでヘッドレス
ブラウザでアクセスする。

使い方:
    uv run python scripts/grok_login.py

    (Chromium ウィンドウが開く → ログイン → ターミナルで Enter)

x.ai が Cloudflare で Block する場合は **本スクリプトを使わずに**
``scripts/grok_extract_cookies.py`` で普段使い Chrome から直接 cookie を
読み取る方が安全です。Playwright で実プロファイルに触るアプローチ
(``--use-system-profile``) は Chrome の自動ログアウト保護機構によって
拡張機能・セッションが失われたため削除しました。

セキュリティ:
    - state.json + ``data/playwright/profile`` には Cookie / ローカル
      ストレージが含まれる (機密)
    - data/ は .gitignore で default-deny 済み
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

DEFAULT_STATE_PATH = Path("data/playwright/state.json")
DEFAULT_USER_DATA_DIR = Path("data/playwright/profile")
DEFAULT_START_URL = "https://grok.com"
# Playwright channel: "chrome" (実 Chrome) / "chromium" (Playwright 同梱)
# x.com は TLS/HTTP fingerprint を見ているため、実 Chrome を使うと
# サーバ側のボット検知を通しやすい (バンドル Chromium だと submit 時に
# "Unexpected token '<'" の HTML ボット検知ページが返ってくることがある)。
DEFAULT_CHANNEL = "chrome"

# 自動化検知を回避するためのフラグ。実 Chrome に近づける。
_LAUNCH_ARGS = (
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-default-browser-check",
    "--no-first-run",
)

# Playwright がデフォルトで付与するフラグのうち、警告バーを出すもの / fingerprint
# を悪化させるものを抑制する。--no-sandbox は実 Chrome 環境では不要かつ
# 上部に「サポートされていないコマンドラインフラグ」警告を出す。
_IGNORE_DEFAULT_ARGS = (
    "--enable-automation",
    "--no-sandbox",
)

# navigator.webdriver = false にするパッチ (ボット検知の主要シグナル)
_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'languages', {
    get: () => ['ja-JP', 'ja', 'en-US', 'en'],
});
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5],
});
"""

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


async def run(
    state_path: Path,
    start_url: str,
    user_data_dir: Path,
    channel: str | None,
) -> int:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    user_data_dir.mkdir(parents=True, exist_ok=True)

    label = channel or "chromium (bundled)"
    print(f"==> {label} を起動します", flush=True)
    print(f"    profile : {user_data_dir}", flush=True)
    print(f"    state   : {state_path}", flush=True)
    print(f"    URL     : {start_url}", flush=True)

    async with async_playwright() as p:
        # persistent context にすることで Cookie / localStorage / 拡張機能設定
        # 等が user_data_dir に永続化される。x.com のボット検知をかなり回避できる。
        # channel="chrome" を指定すると同梱 Chromium ではなくシステム Chrome を使う
        # → TLS/HTTP fingerprint が実ブラウザになり server-side ボット検知も通しやすい
        launch_kwargs: dict[str, object] = {
            "user_data_dir": str(user_data_dir),
            "headless": False,
            "args": list(_LAUNCH_ARGS),
            "user_agent": _USER_AGENT,
            "viewport": {"width": 1280, "height": 800},
            "locale": "ja-JP",
            "timezone_id": "Asia/Tokyo",
            "ignore_default_args": list(_IGNORE_DEFAULT_ARGS),
        }
        if channel:
            launch_kwargs["channel"] = channel

        try:
            context = await p.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as e:  # noqa: BLE001
            if channel:
                print(
                    f"==> {channel} の起動に失敗 ({type(e).__name__}: {e})。",
                    "    bundled Chromium にフォールバックします。",
                    sep="\n",
                    flush=True,
                )
                launch_kwargs.pop("channel", None)
                context = await p.chromium.launch_persistent_context(**launch_kwargs)
            else:
                raise

        # navigator.webdriver = undefined を全ページで適用
        await context.add_init_script(_STEALTH_INIT_SCRIPT)

        # 既存ページがあれば最初のものを使う、なければ新規作成
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await page.goto(start_url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:  # noqa: BLE001
            # ナビゲーション失敗してもページは開いているので続行可能
            print(
                f"==> 警告: 初回ナビゲーションでエラー ({e})。手動で URL を入れてください。",
                flush=True,
            )

        print(
            "\n==> ブラウザでログインしてください。",
            "    入力できない場合は、ブラウザ右上の URL バーで",
            "    別ページ (例: https://grok.com) に手動で移動してください。",
            "    完了したらこのターミナルで Enter を押すと state を保存します。",
            sep="\n",
            flush=True,
        )
        await asyncio.to_thread(input, "")

        # storage_state を抽出 (persistent context なので user_data_dir 自体にも残るが、
        # GrokFetcher は state.json を参照するので保存しておく)
        await context.storage_state(path=str(state_path))
        print(f"==> state を保存しました: {state_path}", flush=True)
        await context.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="grok-login",
        description="Grok 初回ログインで Playwright セッション state を保存する",
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help=f"state 保存先 (default: {DEFAULT_STATE_PATH})",
    )
    parser.add_argument(
        "--user-data-dir",
        type=Path,
        default=DEFAULT_USER_DATA_DIR,
        help=(
            f"Chromium プロファイルディレクトリ (default: {DEFAULT_USER_DATA_DIR})。"
            f" 永続化しておくと 2 回目以降のログインを省略できる"
        ),
    )
    parser.add_argument(
        "--start-url",
        default=DEFAULT_START_URL,
        help=f"起動時に開く URL (default: {DEFAULT_START_URL})",
    )
    parser.add_argument(
        "--channel",
        default=DEFAULT_CHANNEL,
        help=(
            f"Playwright channel (default: {DEFAULT_CHANNEL}). "
            f"'chrome' / 'msedge' は実ブラウザを使い fingerprint を通しやすい。"
            f" 空文字 '' を渡すと bundled Chromium を使う"
        ),
    )
    args = parser.parse_args(argv)

    return asyncio.run(
        run(
            args.state_path,
            args.start_url,
            args.user_data_dir,
            args.channel or None,
        ),
    )


if __name__ == "__main__":
    sys.exit(main())
