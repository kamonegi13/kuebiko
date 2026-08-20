"""ドメイン別本文コンテナ pre-trim (pretrim_main_content) のテスト。

2026-08-21: The Register 改装で trafilatura が本文でなくナビ/ティーザー列
(全記事同一の「MOST POPULAR」列) を約 2 か月抽出していた回帰の固定。
"""

from __future__ import annotations

import trafilatura

from src.tools.content_extractor import pretrim_main_content

# The Register 改装後の構造を模した fixture: toplist ナビ列 + k5a-article 本文
_NAV_TEASER = (
    "<div class='column articlesByTag toplist'>"
    "<h4>MOST POPULAR</h4>"
    "<article class='column small-12'><a>Teaser one about unrelated topic</a>"
    "<p>Teaser body text that is long enough to look like content. " * 8 + "</p></article>"
    "<article class='column small-12'><a>Teaser two about another topic</a>"
    "<p>More teaser text that pollutes generic extraction badly. " * 8 + "</p></article>"
    "</div>"
)
_MAIN_BODY = (
    "<div class='l4 article site_theregister k5a-article'>"
    "<h1>Ransomware crew names a new victim</h1>"
    "<p>The actual article body sentence one with real reporting content. " * 12 + "</p></div>"
)
_REGISTER_HTML = f"<html><head><title>t</title></head><body>{_NAV_TEASER}{_MAIN_BODY}</body></html>"
_REGISTER_URL = "https://www.theregister.com/security/2026/08/20/some-article/5290560"


def test_pretrim_selects_main_article_subtree_for_register_host() -> None:
    # Act
    trimmed = pretrim_main_content(_REGISTER_URL, _REGISTER_HTML)

    # Assert
    assert "actual article body" in trimmed
    assert "MOST POPULAR" not in trimmed
    assert "Teaser one" not in trimmed


def test_pretrim_passes_through_for_non_target_host() -> None:
    # Act
    trimmed = pretrim_main_content("https://feeds.kuebiko.example/a", _REGISTER_HTML)

    # Assert: 対象外ドメインは全文のまま
    assert trimmed == _REGISTER_HTML


def test_pretrim_fails_open_when_selector_misses() -> None:
    # Arrange: 対象ドメインだが本文コンテナが無い (さらに改装された想定)
    html = "<html><body><div class='other'><p>whole page</p></div></body></html>"

    # Act
    trimmed = pretrim_main_content(_REGISTER_URL, html)

    # Assert: fail-open で全文のまま (汎用抽出へ)
    assert trimmed == html


def test_pretrim_fails_open_on_unparseable_html() -> None:
    # Act
    trimmed = pretrim_main_content(_REGISTER_URL, "")

    # Assert
    assert trimmed == ""


def test_trafilatura_on_pretrimmed_html_returns_article_not_nav() -> None:
    """本回帰の end-to-end 固定: pre-trim 済み入力なら抽出結果は本文になる。"""
    # Act
    text = (
        trafilatura.extract(
            pretrim_main_content(_REGISTER_URL, _REGISTER_HTML),
            include_comments=False,
            include_tables=False,
            favor_recall=False,
            deduplicate=False,
        )
        or ""
    )

    # Assert
    assert "actual article body" in text
    assert "MOST POPULAR" not in text
