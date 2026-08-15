"""_trim_html_for_llm の前処理ロジックの unit test。

回帰防止: nav/header/footer chrome を除去し <main> を優先抽出することで、
body 後方に記事リストを持つ CMS (MERICS 等) でも 12KB budget 内に
記事リストが収まることを保証する。
"""

from src.ui.api._source_html_preview import HTML_FOR_LLM_MAX, _trim_html_for_llm
from src.ui.api._source_http import describe_blocked_status, detect_bot_vendor


# ───────── describe_blocked_status: 403 を「相手のボット対策」と明示 ─────────
def test_blocked_detects_perimeterx_in_body() -> None:
    msg = describe_blocked_status(
        403, '<meta name="description" content="px-captcha">', {}, "https://www.humansecurity.com/x"
    )
    assert "PerimeterX" in msg and "403" in msg and "humansecurity.com" in msg


def test_blocked_detects_perimeterx_in_cookie_header() -> None:
    msg = describe_blocked_status(
        403, "denied", {"set-cookie": "_pxhd=abc; path=/"}, "https://a.example/"
    )
    assert "PerimeterX" in msg


def test_blocked_detects_cloudflare() -> None:
    msg = describe_blocked_status(
        403, "Just a moment...", {"cf-ray": "abc123"}, "https://b.example/"
    )
    assert "Cloudflare" in msg and "403" in msg


def test_blocked_generic_403_without_vendor() -> None:
    msg = describe_blocked_status(403, "Forbidden", {}, "https://c.example/")
    assert "403" in msg and "PerimeterX" not in msg and "Cloudflare" not in msg


def test_blocked_429_and_404_messages() -> None:
    assert "429" in describe_blocked_status(429, "", {}, "https://d.example/")
    assert "404" in describe_blocked_status(404, "", {}, "https://e.example/")


# ───────── 他ベンダも検出される (PerimeterX/Cloudflare だけではない) ─────────
def test_detect_other_vendors() -> None:
    assert detect_bot_vendor("", {"x-datadome": "protected"}) == "DataDome"
    assert detect_bot_vendor("", {"set-cookie": "_abck=xyz; ak_bmsc=1"}) == "Akamai Bot Manager"
    assert detect_bot_vendor("", {"set-cookie": "incap_ses_1=a"}) == "Imperva (Incapsula)"
    assert detect_bot_vendor("The requested URL was rejected", {}) == "F5 / Shape"
    assert detect_bot_vendor("", {"x-amzn-waf-action": "block"}) == "AWS WAF"


def test_detect_returns_none_when_no_fingerprint() -> None:
    # 痕跡が無ければ名前を捏造しない。
    assert detect_bot_vendor("Forbidden", {"server": "nginx"}) is None


def test_perimeterx_wins_over_cloudflare_when_both_present() -> None:
    # HUMAN は CF(CDN) + PerimeterX(bot) の二重。固有の PerimeterX を優先する。
    vendor = detect_bot_vendor("px-captcha", {"cf-ray": "abc", "set-cookie": "_pxhd=1"})
    assert vendor == "PerimeterX (HUMAN)"


def test_other_vendor_403_message_names_vendor() -> None:
    msg = describe_blocked_status(403, "", {"x-datadome": "1"}, "https://f.example/")
    assert "DataDome" in msg and "ボット対策" in msg


def test_strips_chrome_tags() -> None:
    # Arrange
    html = (
        "<html><body>"
        "<header><a href='/login'>login</a></header>"
        "<nav><a href='/about'>about</a></nav>"
        "<main><a href='/article-1'>news</a></main>"
        "<footer><a href='/terms'>terms</a></footer>"
        "</body></html>"
    )

    # Act
    out = _trim_html_for_llm(html)

    # Assert
    assert "/article-1" in out
    assert "/login" not in out
    assert "/about" not in out
    assert "/terms" not in out


def test_prefers_main_region_over_body() -> None:
    # Arrange
    html = "<body><div>boilerplate</div><main><a href='/post'>post</a></main></body>"

    # Act
    out = _trim_html_for_llm(html)

    # Assert
    assert "boilerplate" not in out
    assert "/post" in out


def test_falls_back_to_body_without_main() -> None:
    # Arrange
    html = "<body><div class='list'><a href='/x'>x</a></div></body>"

    # Act
    out = _trim_html_for_llm(html)

    # Assert
    assert "/x" in out


def test_article_list_after_large_chrome_survives_truncation() -> None:
    # Arrange: MERICS 型 — 巨大 header の後に <main> 記事リストが来る
    big_header = "<header>" + ("<a href='/nav'>x</a>" * 2000) + "</header>"
    html = (
        "<body>"
        + big_header
        + "<main><div class='views-row'><a href='/en/report/target'>R</a></div></main>"
        + "</body>"
    )
    assert len(big_header) > HTML_FOR_LLM_MAX  # chrome 単体で budget 超過

    # Act
    out = _trim_html_for_llm(html)

    # Assert: chrome を捨てたので記事リンクが budget 内に残る
    assert "/en/report/target" in out
    assert "views-row" in out


def test_truncates_to_budget() -> None:
    # Arrange
    html = "<body><main>" + ("a" * (HTML_FOR_LLM_MAX + 5000)) + "</main></body>"

    # Act
    out = _trim_html_for_llm(html)

    # Assert
    assert out.endswith("... [truncated]")
    assert len(out) <= HTML_FOR_LLM_MAX + len("... [truncated]")


class TestCardLayoutTitles:
    """カード型 (画像ラッパ a + 見出し a) で無題にならないこと。

    ENISA 実例: 1 記事に空の a と h3>a の 2 本が張られ、先勝ちだと全記事が無題になった。
    """

    HTML = """
    <div class="featured-items">
      <div class="card">
        <div><a href="/news/alpha-report"><img src="x.png"></a></div>
        <h3><a href="/news/alpha-report">Alpha Report on Threats</a></h3>
      </div>
      <div class="card">
        <div><a href="/news/beta-brief"><img src="y.png"></a></div>
        <h3><a href="/news/beta-brief">Beta Brief</a></h3>
      </div>
      <a href="/topics/tagging">Tagging</a>
    </div>
    """

    def _apply(self, selector: str, scope: str = "") -> list[tuple[str, str]]:
        from src.ui.api._source_html_preview import _apply_selectors_html

        arts = _apply_selectors_html(
            self.HTML, selector, "h3 a", "https://e.example/news", 10, scope
        )
        return [(a.title, a.url) for a in arts]

    def test_empty_wrapper_anchor_does_not_win(self) -> None:
        out = dict((u, t) for t, u in self._apply(".featured-items a[href*='/news/']"))
        assert out["https://e.example/news/alpha-report"] == "Alpha Report on Threats"
        assert out["https://e.example/news/beta-brief"] == "Beta Brief"

    def test_scope_pattern_drops_non_article_links(self) -> None:
        urls = [u for _t, u in self._apply(".featured-items a", scope="/news/")]
        assert all("/news/" in u for u in urls)
        assert "https://e.example/topics/tagging" not in urls

    def test_without_scope_tag_links_are_kept(self) -> None:
        # 絞り込み無しなら記事以外も入る = 範囲指定が必要という前提の裏取り
        urls = [u for _t, u in self._apply(".featured-items a")]
        assert "https://e.example/topics/tagging" in urls
