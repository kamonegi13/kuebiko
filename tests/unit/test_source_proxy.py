"""_source_proxy の sanitize / inject / JS-shell 判定の unit test。"""

from src.ui.api._source_proxy import (
    looks_like_js_shell,
    sanitize_and_inject,
)

_PICKER_JS = "/* picker */ console.log('picker');"


def test_strips_script_tags() -> None:
    html = "<html><head></head><body><script>evil()</script><a href='/x'>x</a></body></html>"
    out = sanitize_and_inject(html, "https://example.com/list", _PICKER_JS)
    assert "evil()" not in out
    # picker script は注入される
    assert "/* picker */" in out


def test_strips_event_handlers_and_js_href() -> None:
    html = (
        "<html><body>"
        "<a href='javascript:steal()' onclick='steal()'>bad</a>"
        "<div onmouseover='x()'>d</div>"
        "</body></html>"
    )
    out = sanitize_and_inject(html, "https://example.com/", _PICKER_JS)
    assert "onclick" not in out
    assert "onmouseover" not in out
    assert "javascript:" not in out


def test_strips_nested_browsing_context_tags() -> None:
    # security-review M4: iframe/object/embed/frame は隔離 iframe 内でも除去 (多層防御)
    html = (
        "<html><body>"
        "<iframe src='https://evil.example/'></iframe>"
        "<object data='x.swf'></object>"
        "<embed src='y'></embed>"
        "<svg><script>evil()</script></svg>"
        "<a href='/ok'>ok</a>"
        "</body></html>"
    )
    out = sanitize_and_inject(html, "https://example.com/", _PICKER_JS)
    assert "<iframe" not in out
    assert "<object" not in out
    assert "<embed" not in out
    assert "evil()" not in out  # svg 内 script も再帰除去
    assert "/ok" in out  # 通常リンクは保持


def test_strips_obfuscated_and_vbscript_schemes() -> None:
    # 制御文字での難読化 (java\tscript:) と vbscript:、src 属性も対象
    html = (
        "<html><body>"
        "<a href='java\tscript:bad()'>a</a>"
        "<a href='VBScript:bad()'>b</a>"
        "<img src='javascript:bad()'>"
        "</body></html>"
    )
    out = sanitize_and_inject(html, "https://example.com/", _PICKER_JS)
    low = out.lower()
    assert "script:bad" not in low.replace("\t", "")
    assert "vbscript:" not in low


def test_preserves_visual_structure() -> None:
    # 隔離は origin 分離で行うため、見た目に必要な class/id/style/img/link は保持する
    html = (
        "<html><head><link rel='stylesheet' href='/site.css'>"
        "<style>.a{color:red}</style></head><body>"
        "<div class='views-row' id='r1' style='margin:4px'>"
        "<img src='/logo.png' width='40'><a href='/post'>title</a></div>"
        "</body></html>"
    )
    out = sanitize_and_inject(html, "https://example.com/", _PICKER_JS)
    assert "views-row" in out
    assert 'id="r1"' in out or "id='r1'" in out
    assert "stylesheet" in out  # 外部 CSS link は保持 (視覚再現)
    assert "<style" in out
    assert "/logo.png" in out


def test_strips_meta_http_equiv() -> None:
    html = (
        "<html><head>"
        "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'\">"
        "<meta http-equiv='refresh' content='0;url=/x'>"
        "<meta name='keep' content='ok'>"
        "</head><body><a href='/y'>y</a></body></html>"
    )
    out = sanitize_and_inject(html, "https://example.com/", _PICKER_JS)
    assert "http-equiv" not in out
    assert 'name="keep"' in out or "name='keep'" in out


def test_injects_base_href() -> None:
    html = "<html><head></head><body><a href='/z'>z</a></body></html>"
    out = sanitize_and_inject(html, "https://merics.org/en/analysis", _PICKER_JS)
    assert "<base" in out
    assert "https://merics.org/" in out


def test_picker_injected_before_body_end() -> None:
    html = "<html><body><p>hi</p></body></html>"
    out = sanitize_and_inject(html, "https://example.com/", _PICKER_JS)
    body_close = out.rfind("</body>")
    picker_pos = out.find("/* picker */")
    assert 0 < picker_pos < body_close


def test_force_visible_reveal_elements() -> None:
    """AOS 等 scroll-reveal で隠れた記事カードを強制表示する CSS を注入 (NCSC NZ 対策)。"""
    html = (
        "<html><head></head><body>"
        "<a class='card__link' data-aos='fade-up' href='/news/x'>記事</a>"
        "</body></html>"
    )
    out = sanitize_and_inject(html, "https://www.ncsc.govt.nz/news/", _PICKER_JS)
    # data-aos 要素を opacity:1 / visibility:visible で強制表示する style が注入される
    assert "[data-aos]" in out
    assert "opacity:1!important" in out
    assert "visibility:visible!important" in out
    # 記事カード自体は DOM に残っている (除去しない)
    assert "data-aos='fade-up'" in out or 'data-aos="fade-up"' in out


def test_js_shell_detection_empty_root() -> None:
    spa = '<html><body><div id="root"></div></body></html>'
    assert looks_like_js_shell(spa) is True


def test_js_shell_detection_rich_page() -> None:
    rich = (
        "<html><body>"
        + "".join(f"<a href='/a{i}'>art {i}</a>" for i in range(10))
        + "</body></html>"
    )
    assert looks_like_js_shell(rich) is False
