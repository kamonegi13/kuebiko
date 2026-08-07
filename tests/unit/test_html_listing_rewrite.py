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
