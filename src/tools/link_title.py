"""記事一覧ページのリンクから表示タイトルを解決する (HTML scraper 共通)。

カード型レイアウトでは 1 記事に **複数のリンク** が張られる (画像を包む空の a と、
見出しの中の a)。素朴に「最初に一致した a のテキスト」を取ると、空のラッパ a を
掴んで全記事が無題になる (ENISA で実際に発生)。

そこでリンク単体でなく **リンクが属するカード** まで視野を広げてタイトルを探す。
探索順は「指定されたルール → リンク自身 → 近い見出し → 代替テキスト → URL の slug」。
最後まで見つからなくても URL slug を返すので、無題の記事は原則生まれない。

preview (登録ウィザード) と runtime (watcher) の双方から呼び、**見えているものと
取り込まれるものを一致させる**。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

__all__ = ["is_better_title", "resolve_link_title", "title_from_url"]

# カードの外側をどこまで遡るか (これ以上遡ると別記事のテキストを拾い始める)。
_MAX_ANCESTORS = 3
_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_MAX_TITLE_LEN = 200


def title_from_url(url: str) -> str:
    """URL の末尾 slug を人が読める形にする (最後の手段)。"""
    path = urlparse(url).path.rstrip("/")
    slug = path.rsplit("/", 1)[-1] if path else ""
    slug = re.sub(r"\.(html?|php|aspx?)$", "", slug, flags=re.IGNORECASE)
    words = [w for w in re.split(r"[-_]+", slug) if w]
    return " ".join(words)[:_MAX_TITLE_LEN]


def _text(el: Any) -> str:
    try:
        return str(el.get_text(" ", strip=True))[:_MAX_TITLE_LEN]
    except Exception:  # noqa: BLE001 — 壊れた DOM で抽出全体を止めない
        return ""


def _select_one(el: Any, selector: str) -> Any:
    try:
        return el.select_one(selector)
    except Exception:  # noqa: BLE001 — 不正な selector は「見つからない」と同じ扱い
        return None


def _ancestors(el: Any) -> list[Any]:
    out: list[Any] = []
    node = getattr(el, "parent", None)
    while node is not None and len(out) < _MAX_ANCESTORS:
        out.append(node)
        node = getattr(node, "parent", None)
    return out


def resolve_link_title(el: Any, title_selector: str = "", *, url: str = "") -> str:
    """リンク要素から表示タイトルを決める。見つからなければ URL slug。"""
    if title_selector:
        # 指定ルールはリンク自身にもカード側にも当てる (LLM/人は「カード内の見出し」の
        # つもりで h3 a のようなルールを書くため、リンク基準だけだと必ず外れる)。
        for scope in (el, *_ancestors(el)):
            found = _select_one(scope, title_selector)
            if found is not None:
                text = _text(found)
                if text:
                    return text
    own = _text(el)
    if own:
        return own
    for scope in _ancestors(el):
        for tag in _HEADING_TAGS:
            found = _select_one(scope, tag)
            if found is not None:
                text = _text(found)
                if text:
                    return text
    for attr in ("title", "aria-label"):
        try:
            value = el.get(attr)
        except Exception:  # noqa: BLE001
            value = None
        if value:
            return str(value)[:_MAX_TITLE_LEN]
    img = _select_one(el, "img[alt]")
    if img is not None:
        alt = img.get("alt")
        if alt:
            return str(alt)[:_MAX_TITLE_LEN]
    return title_from_url(url)


def is_better_title(new: str, current: str, *, url: str = "") -> bool:
    """同一 URL が複数回出たとき、新しい候補で上書きすべきか。

    カード型では **空ラッパ a が先に一致する** ため、先勝ちにすると無題で固定される。
    URL 由来の代替より実テキストを優先する。
    """
    if not new:
        return False
    if not current:
        return True
    fallback = title_from_url(url)
    return bool(fallback) and current == fallback and new != fallback
