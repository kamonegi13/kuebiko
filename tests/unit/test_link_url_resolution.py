"""一覧ページの相対 href を絶対 URL に解決する規則のテスト (2026-08-16)。

核心の不変量:
- ``<base href>`` が宣言されていれば **取得元 URL ではなくそちら** が基準 (HTML 標準)
- preview と runtime が同じ規則を共有する (見えているもの = 取り込まれるもの)

実害: BSI Pressemitteilungen は href が先頭スラッシュ無しで <base> が宣言されており、
取得元 (.../SiteGlobals/Forms/Suche/...) を基準にしたため全 URL が 404 になっていた。
タイトルと URL は取れるので一覧層は正常に見え、本文取得だけが静かに全滅する。
"""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from src.tools.link_title import base_href_of, resolve_link_url

BSI_PAGE = (
    "https://www.bsi.bund.de/SiteGlobals/Forms/Suche/"
    "Expertensuche_Pressemitteilungen_Formular.html?nn=520690"
)
BSI_HREF = "DE/Service-Navi/Presse/Pressemitteilungen/Presse2026/260806_security_txt.html"


class TestBaseHrefOf:
    def test_reads_base_href(self) -> None:
        soup = BeautifulSoup(
            '<html><head><base href="https://x.example/"/></head></html>', "html.parser"
        )

        assert base_href_of(soup) == "https://x.example/"

    def test_absent_base_returns_empty(self) -> None:
        soup = BeautifulSoup("<html><head></head></html>", "html.parser")

        assert base_href_of(soup) == ""


class TestResolveLinkUrl:
    def test_bsi_case_uses_base_not_page(self) -> None:
        """実データ: これを取り違えると全記事が 404 になる。"""
        resolved = resolve_link_url(BSI_HREF, BSI_PAGE, "https://www.bsi.bund.de/")

        assert resolved == (
            "https://www.bsi.bund.de/DE/Service-Navi/Presse/"
            "Pressemitteilungen/Presse2026/260806_security_txt.html"
        )

    def test_without_base_falls_back_to_page_url(self) -> None:
        resolved = resolve_link_url("article/1.html", "https://x.example/news/index.html", "")

        assert resolved == "https://x.example/news/article/1.html"

    def test_root_relative_href_is_unaffected_by_base(self) -> None:
        """先頭スラッシュ付きは元から正しく解決されていた (退行させない)。"""
        page = "https://x.example/deep/path/index.html"

        assert (
            resolve_link_url("/a/b.html", page, "https://x.example/")
            == "https://x.example/a/b.html"
        )
        assert resolve_link_url("/a/b.html", page, "") == "https://x.example/a/b.html"

    @pytest.mark.parametrize("href", ["https://other.example/x", "//cdn.example/y"])
    def test_absolute_href_is_preserved(self, href: str) -> None:
        resolved = resolve_link_url(href, BSI_PAGE, "https://www.bsi.bund.de/")

        assert resolved.endswith(href.lstrip("/")) or resolved == href
