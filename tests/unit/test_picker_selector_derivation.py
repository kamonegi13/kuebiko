"""picker-overlay.js の deriveSelector アルゴリズムの回帰テスト。

picker-overlay.js は browser JS で JS テスト基盤が無いため、ここでは
deriveSelector の**ロジックを Python に忠実移植**し (querySelectorAll →
BeautifulSoup .select())、MERICS で発覚した失敗モードを再現する合成
フィクスチャで不変条件を固定する:

  「クリックした anchor に近い階層で、複数記事に汎化しつつ最も具体的な
   単一 class selector を選ぶ。'a' (ページ全リンク) のような広すぎる
   selector は返さない」

旧版は distinct 最大を選び 'a' が常勝するバグがあった (commit 8517c23)。
JS 側 (src/ui/static/picker-overlay.js) を変更したらこのロジックも追従させること。
"""

import re
from typing import NamedTuple

from bs4 import BeautifulSoup, Tag

_SEMANTIC = re.compile(
    r"title|headline|field--name|post|entry|teaser|card|story|article|views-row|item",
    re.I,
)


def _stable(c: str) -> bool:
    if not c:
        return False
    if re.match(r"^(css|sc|jsx)-", c):
        return False
    return not re.match(r"^[a-z]*[0-9a-f]{6,}$", c, re.I)


class _SelStat(NamedTuple):
    n_matches: int
    distinct: int
    has_target: bool


def _eval(soup: BeautifulSoup, sel: str, target_href: str) -> _SelStat | None:
    try:
        els = soup.select(sel)
    except Exception:  # noqa: BLE001
        return None
    hrefs: set[str] = set()
    has_target = False
    for el in els:
        a = el if el.name == "a" else el.find("a", href=True)
        if isinstance(a, Tag) and a.get("href"):
            href = str(a.get("href"))
            hrefs.add(href)
            if href == target_href:
                has_target = True
    return _SelStat(n_matches=len(els), distinct=len(hrefs), has_target=has_target)


def derive_selector(soup: BeautifulSoup, target: Tag) -> str:
    """picker-overlay.js deriveSelector の Python 移植。"""
    target_href = str(target.get("href"))
    total_anchors = len(soup.find_all("a", href=True))
    max_reasonable = min(60, int(total_anchors * 0.6))
    cur: Tag | None = target

    for _ in range(9):
        if cur is None or cur.name == "body":
            break
        classes = [c for c in (cur.get("class") or []) if _stable(c)]
        cands = []
        for c in classes:
            sel = f"a.{c}" if cur.name == "a" else f".{c} a"
            r = _eval(soup, sel, target_href)
            if not r or not r.has_target:
                continue
            if r.distinct < 2 or r.n_matches > max_reasonable:
                continue
            cands.append((not bool(_SEMANTIC.search(c)), r.distinct, sel))
        if cands:
            cands.sort()
            return cands[0][2]
        cur = cur.parent if isinstance(cur.parent, Tag) else None
    return target.name


def _merics_like_fixture() -> str:
    """type-variant カード (各 card で type class が異なる) + 大量 nav リンク。

    全 card 共通の title 構造は `.views-row .field--name-title a`。
    各 card 先頭にはカテゴリバッジ link があり、誤って拾われやすい。
    """
    nav = "<nav>" + "".join(f"<a href='/nav{i}'>n{i}</a>" for i in range(20)) + "</nav>"
    cards = ""
    types = ["microsite", "podcast", "report", "comment", "microsite", "report"]
    for i, t in enumerate(types):
        cards += (
            f"<div class='views-row'><article class='content--type-{t}'>"
            f"<span class='field field--name-field-topic'><a href='/topic-{t}'>{t}</a></span>"
            f"<span class='field field--name-title node-title'>"
            f"<a href='/article-{i}'>Article {i} title</a></span>"
            "</article></div>"
        )
    footer = "<footer>" + "".join(f"<a href='/f{i}'>f{i}</a>" for i in range(10)) + "</footer>"
    return f"<html><body>{nav}<main>{cards}</main>{footer}</body></html>"


def test_derives_title_field_selector_not_broad_a() -> None:
    soup = BeautifulSoup(_merics_like_fixture(), "html.parser")
    # 3 番目の card のタイトルをクリック
    target = soup.select(".views-row")[2].select_one(".field--name-title a")
    assert target is not None

    sel = derive_selector(soup, target)

    # 広すぎる 'a' を返さない
    assert sel != "a"
    # title-field を捉えた selector
    assert "field--name-title" in sel or "node-title" in sel
    # その selector が全 6 記事をクリーン抽出する (カテゴリバッジは含まない)
    matched = {str(a.get("href")) for a in soup.select(sel)}
    assert matched == {f"/article-{i}" for i in range(6)}


def test_does_not_pick_type_variant_class() -> None:
    # type-variant (content--type-microsite) は card ごとに違うので汎化しない
    soup = BeautifulSoup(_merics_like_fixture(), "html.parser")
    target = soup.select_one(".views-row .field--name-title a")
    assert target is not None
    sel = derive_selector(soup, target)
    assert "content--type-" not in sel


def test_falls_back_when_no_class() -> None:
    # class が一切無い単純構造でも例外を出さず anchor を返す
    soup = BeautifulSoup("<html><body><div><a href='/x'>x</a></div></body></html>", "html.parser")
    target = soup.find("a")
    assert isinstance(target, Tag)
    sel = derive_selector(soup, target)
    assert isinstance(sel, str) and sel
