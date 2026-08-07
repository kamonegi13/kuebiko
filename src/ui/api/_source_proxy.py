"""Visual selector picker 用 page proxy。

ユーザ指定 URL を取得 (静的 or JS 描画) し、same-origin で iframe 配信できるよう
sanitize + base href + picker JS 注入する。元サイトの <script> は実行させない。

セキュリティ (CLAUDE.md §4 / §12):
- 呼び出し前に url_guard.assert_safe_public_url で SSRF 検証する
- READ_ONLY instance では endpoint 自体を無効化 (open proxy 化防止) — app.py 側で制御
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from src.tools.url_guard import assert_safe_public_url

_PICKER_JS_PATH = Path(__file__).resolve().parent.parent / "static" / "picker-overlay.js"

# 静的取得 HTML が JS-shell (SPA mount 点のみ) かの判定閾値
_JS_SHELL_LINK_THRESHOLD = 3

# 実行可能 / 入れ子ブラウジングコンテキストを生む要素 (security-review M4 多層防御)。
# base は後段で自前注入するため元ページのものは捨てる。
_DANGEROUS_TAGS = ("script", "iframe", "object", "embed", "applet", "frame", "frameset", "base")
# URL を取りうる属性 (危険スキーム除去対象)
_URL_ATTRS = ("href", "src", "xlink:href", "action", "formaction")


def _has_dangerous_scheme(value: str) -> bool:
    """属性値が javascript:/vbscript: スキームか判定する。

    ``java\\tscript:`` のような制御文字・空白による難読化を無効化してから判定する
    (ブラウザは scheme 解析前に制御文字を除去するため、それに合わせる)。
    """
    cleaned = re.sub(r"[\x00-\x20\s]", "", value).lower()
    return cleaned.startswith(("javascript:", "vbscript:"))


def _load_picker_js() -> str:
    return _PICKER_JS_PATH.read_text(encoding="utf-8")


def looks_like_js_shell(html: str) -> bool:
    """静的取得 HTML が JS 描画前の空 shell かを推定する。

    本文リンクが極端に少ない / SPA の典型 mount 点だけ、を JS-shell とみなす。
    """
    lowered = html.lower()
    soup = BeautifulSoup(html, "html.parser")
    body = soup.find("body")
    link_count = len(body.find_all("a", href=True)) if isinstance(body, Tag) else 0
    if link_count <= _JS_SHELL_LINK_THRESHOLD:
        return True
    # 典型的な SPA mount 点 + 本文がほぼ空
    for marker in ('id="root"', 'id="app"', 'id="__next"', "ng-app"):
        if marker in lowered and link_count <= _JS_SHELL_LINK_THRESHOLD * 2:
            return True
    return False


def sanitize_and_inject(html: str, page_url: str, picker_js: str) -> str:
    """proxied HTML を iframe 配信用に無害化し、base href + picker JS を注入する。

    - script / iframe / object / embed / applet / frame / 元 base を全除去
    - on* イベントハンドラ属性 + javascript:/vbscript: URL を除去
    - <meta http-equiv> (CSP / refresh) 除去
    - <base href> 注入 (相対 CSS / 画像を元サイトから読み込む)
    - picker JS を </body> 直前にインライン注入

    注: iframe 側は sandbox="allow-scripts" (allow-same-origin なし) の opaque origin で
    隔離されるため、このサニタイズは多層防御 (security-review M4)。隔離が一次防御。
    """
    soup = BeautifulSoup(html, "html.parser")

    # 実行可能 / フレーム生成タグを全除去 (find_all は再帰なので svg/math 内の <script> も対象)
    for tag in soup.find_all(_DANGEROUS_TAGS):
        tag.decompose()
    for tag in soup.find_all("meta"):
        if tag.has_attr("http-equiv"):
            tag.decompose()
    # on* インラインハンドラ + 危険スキーム (javascript:/vbscript:) を URL 属性から除去
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        for attr in list(tag.attrs.keys()):
            if attr.lower().startswith("on"):
                del tag[attr]
        for attr in _URL_ATTRS:
            val = tag.get(attr)
            if isinstance(val, str) and _has_dangerous_scheme(val):
                del tag[attr]

    head = soup.find("head")
    if not isinstance(head, Tag):
        head = soup.new_tag("head")
        if soup.html and isinstance(soup.html, Tag):
            soup.html.insert(0, head)
        else:
            soup.insert(0, head)
    # base href は元ページ URL を基準にする (相対リンク / asset 解決用)
    parsed = urlparse(page_url)
    base_origin = urljoin(page_url, "/")
    if not parsed.scheme:
        base_origin = page_url
    base_tag = soup.new_tag("base", href=base_origin)
    head.insert(0, base_tag)

    # picker: 元サイトが AOS / wow 等の scroll-reveal を使うと、要素を初期 opacity:0 にし
    # JS で表示する。proxy は全 JS を除去するため、その JS が走らず記事カードが永久に
    # opacity:0 のまま隠れて選択できない (NCSC NZ 等)。強制表示 CSS を head 末尾に注入して
    # 元サイトの reveal CSS を !important で上書きする (静的/JS 両モードで有効)。
    reveal_fix = soup.new_tag("style")
    reveal_fix.string = (
        "[data-aos],[data-sr],.wow,.aos-init,.reveal,"
        "[style*='opacity:0'],[style*='opacity: 0']"
        "{opacity:1!important;transform:none!important;visibility:visible!important;}"
        "*{animation-duration:0s!important;animation-delay:0s!important;"
        "transition:none!important;}"
    )
    head.append(reveal_fix)

    script_tag = soup.new_tag("script")
    script_tag.string = picker_js
    body = soup.find("body")
    if isinstance(body, Tag):
        body.append(script_tag)
    else:
        soup.append(script_tag)

    return str(soup)


async def build_proxy_html(url: str, render_mode: str = "auto") -> str:
    """url を取得・無害化し、picker 注入済みの same-origin HTML を返す。

    render_mode: "static" | "js" | "auto" (既定)。
    呼び出し側 (endpoint) で READ_ONLY 無効化を済ませること。
    """
    from src.ui.api._source_http import fetch_text

    assert_safe_public_url(url)
    mode = render_mode if render_mode in ("static", "js", "auto") else "auto"

    html = ""
    if mode in ("static", "auto"):
        status, html, _, _ = fetch_text(url, timeout=20.0)
        if status >= 400:
            html = ""

    needs_js = mode == "js" or (mode == "auto" and (not html or looks_like_js_shell(html)))
    if needs_js:
        from src.tools.page_renderer import render_html

        html = await render_html(url)

    if not html:
        raise RuntimeError("page fetch / render に失敗しました")

    # 制御文字や巨大ページの暴走を抑える (5MB 上限)
    if len(html) > 5_000_000:
        html = html[:5_000_000]

    return sanitize_and_inject(html, url, _load_picker_js())
