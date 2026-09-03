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


def test_pretrim_keeps_article_paragraphs_and_drops_nav_for_security_next() -> None:
    """Security NEXT は署名「（Security NEXT - 日付）」より前の <p> だけが本文。

    ⚠ 2026-09-03: 全 1,164 記事で関連記事ナビが本文に混入し、別記事の見出しの
    数値が続報バッジに出た。署名以降 (ツイート/PR/関連リンク/関連記事) を落とす。
    """
    # Arrange — 実ページの構造を縮約した fixture
    html = """
    <html><body><div class="content">
      <div class="prtxt">広告</div>
      <div class="title"><h1>見出し</h1></div>
      <p>本文の第一段落。7件の脆弱性が悪用されている。</p>
      <p>本文の第二段落。</p>
      <div class="pnavi"><a href="https://kuebiko.example/1">次のページ</a></div>
      <p>（Security NEXT - 2026/09/03 ）</p>
      <div class="linkc"><h3>関連リンク</h3></div>
      <p><a href="https://kuebiko.example/2">関連記事: 1万3131件</a></p>
    </div></body></html>
    """

    # Act
    got = pretrim_main_content("https://www.security-next.com/189777", html)

    # Assert — 本文は残り、署名・関連記事は落ちる
    assert "本文の第一段落" in got and "本文の第二段落" in got
    assert "関連記事" not in got
    assert "1万3131件" not in got
    assert "Security NEXT - 2026" not in got


def test_pretrim_without_pagination_still_stops_at_the_signature() -> None:
    """ページ送り (pnavi) の無い短い記事は署名の条件だけで本文を選べる。"""
    # Arrange
    html = """
    <html><body><div class="content">
      <p>短い記事の本文。</p>
      <p>（Security NEXT - 2026/09/03 ）</p>
      <p><a href="https://kuebiko.example/2">関連記事</a></p>
    </div></body></html>
    """

    # Act
    got = pretrim_main_content("https://www.security-next.com/1", html)

    # Assert
    assert "短い記事の本文" in got
    assert "関連記事" not in got
