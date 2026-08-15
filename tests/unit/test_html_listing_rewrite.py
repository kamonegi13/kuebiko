"""html_listing の url_rewrite (監査 2026-08-01 ①: BSI 検索 deeplink 404 の根治)。

BSI の listing は記事 href を検索フォームの deeplink
(`/SiteGlobals/Forms/Suche/DE/...` → 全件 404) で返す。canonical (`/DE/...`) は
200 を返すため、宣言的 regex 書換で正規化する。
"""

from __future__ import annotations

from pathlib import Path

from src.watchers.html_listing import HtmlListingWatcher

_HTML = """
<div class="c-teaser">
  <a class="c-teaser__headline-link"
     href="/SiteGlobals/Forms/Suche/DE/Pressemitteilung/pm-2026.html">BSI warnt</a>
</div>
"""


def _watcher(tmp_path: Path, **kw: str) -> HtmlListingWatcher:
    return HtmlListingWatcher(
        name="bsi-test",
        listing_url="https://www.bsi.bund.de/DE/Service-Navi/Presse/presse_node.html",
        article_link_selector="a.c-teaser__headline-link",
        state_file=tmp_path / "seen.json",
        feed_title="BSI",
        **kw,  # type: ignore[arg-type]
    )


def test_rewrite_strips_search_form_prefix(tmp_path: Path) -> None:
    w = _watcher(
        tmp_path,
        url_rewrite_pattern=r"/SiteGlobals/Forms/Suche",
        url_rewrite_replacement="",
    )
    items = w._apply_selectors(_HTML)
    assert items == [("https://www.bsi.bund.de/DE/Pressemitteilung/pm-2026.html", "BSI warnt")]


def test_no_rewrite_without_pattern(tmp_path: Path) -> None:
    w = _watcher(tmp_path)
    items = w._apply_selectors(_HTML)
    assert items[0][0].startswith("https://www.bsi.bund.de/SiteGlobals/Forms/Suche/")


def test_invalid_rewrite_pattern_fails_open(tmp_path: Path) -> None:
    # 不正 regex は書換なしで続行 (listing 全体を殺さない)
    w = _watcher(tmp_path, url_rewrite_pattern=r"[unclosed", url_rewrite_replacement="")
    items = w._apply_selectors(_HTML)
    assert len(items) == 1


class TestCardLayoutTitle:
    """カード型 (画像ラッパ a + 見出し a) で無題にならないこと (2026-08-15)。

    ENISA 実例: 1 記事に空の a と h3>a が張られ、先勝ちで全記事が無題になっていた。
    preview (登録ウィザード) と同じ規則を runtime でも使う。
    """

    HTML = """
    <div class="items">
      <div class="card">
        <a href="/news/a"><img src="x.png"></a>
        <h3><a href="/news/a">Gamma Advisory</a></h3>
      </div>
    </div>
    """

    def test_title_comes_from_card_not_empty_anchor(self, tmp_path: Path) -> None:
        w = HtmlListingWatcher(
            name="card-test",
            listing_url="https://e.example/news",
            article_link_selector=".items a[href*='/news/']",
            title_selector="h3 a",
            state_file=tmp_path / "seen.json",
            feed_title="E",
        )
        assert w._apply_selectors(self.HTML) == [("https://e.example/news/a", "Gamma Advisory")]

    def test_slug_fallback_instead_of_url(self, tmp_path: Path) -> None:
        # タイトルがどこにも無いときも URL そのものでなく slug を人が読める形で出す
        w = HtmlListingWatcher(
            name="slug-test",
            listing_url="https://e.example/news",
            article_link_selector="a",
            state_file=tmp_path / "seen.json",
            feed_title="E",
        )
        out = w._apply_selectors('<a href="/news/some-long-title"><img src="x"></a>')
        assert out == [("https://e.example/news/some-long-title", "some long title")]
