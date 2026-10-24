"""Grok 投稿の外部リンク取得 (src/grok/link_fetch.py) のテスト。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from src.grok.link_fetch import (
    LinkedPage,
    LinkFetchConfig,
    LinkFetcher,
    build_link_section,
    expand_url,
    link_fetch_mode,
    linked_context_json,
    select_candidates,
)
from src.tools.content_extractor import ExtractionResult

_PUBLIC_IP = "93.184.216.34"
_CFG = LinkFetchConfig(max_links=2, max_hops=4, max_chars=100, min_chars=50)
_LONG = "本文 " * 60  # 180 字超


@pytest.fixture(autouse=True)
def _resolve_everything_as_public(monkeypatch: pytest.MonkeyPatch) -> None:
    """DNS を使わない。IP リテラルだけは実際の判定 (private → 拒否) に通す。"""
    import ipaddress

    def fake(host: str) -> list[str]:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return [_PUBLIC_IP]
        return [host]

    monkeypatch.setattr("src.tools.url_guard._resolve_ips", fake)


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


class _StubExtractor:
    def __init__(self, results: dict[str, ExtractionResult], delay: float = 0.0) -> None:
        self._results = results
        self._delay = delay
        self.calls: list[str] = []

    async def extract(self, url: str) -> ExtractionResult:
        self.calls.append(url)
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._results.get(
            url, ExtractionResult(url=url, success=False, failure_reason="http_error_404")
        )


def _ok(url: str, text: str = _LONG) -> ExtractionResult:
    return ExtractionResult(url=url, text=text, success=True)


# ---------- mode ----------


def test_mode_defaults_to_shadow_and_rejects_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROK_LINK_FETCH", raising=False)
    assert link_fetch_mode() == "shadow"
    monkeypatch.setenv("GROK_LINK_FETCH", "on")
    assert link_fetch_mode() == "on"
    monkeypatch.setenv("GROK_LINK_FETCH", "off")
    assert link_fetch_mode() == "off"
    monkeypatch.setenv("GROK_LINK_FETCH", "bogus")
    assert link_fetch_mode() == "shadow"


# ---------- redirect expansion ----------


def test_expand_follows_tco_redirect_chain() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "t.co":
            return httpx.Response(301, headers={"location": "https://bit.ly/x"})
        if request.url.host == "bit.ly":
            return httpx.Response(302, headers={"location": "https://news.example/final"})
        return httpx.Response(200, text="ok")

    result = asyncio.run(expand_url("https://t.co/abc", client=_client(handler), max_hops=4))

    assert result.final_url == "https://news.example/final"
    assert result.reason is None


def test_expand_leaves_non_shortener_urls_untouched() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 呼ばれない
        raise AssertionError("non-shortener must not be requested")

    result = asyncio.run(
        expand_url("https://example.com/post", client=_client(handler), max_hops=4)
    )

    assert result.final_url == "https://example.com/post"


def test_expand_stops_at_hop_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"location": "https://t.co/loop"})

    result = asyncio.run(expand_url("https://t.co/loop", client=_client(handler), max_hops=3))

    assert result.final_url is None
    assert result.reason == "too_many_redirects"


def test_expand_refuses_redirect_into_private_address() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})

    result = asyncio.run(expand_url("https://t.co/evil", client=_client(handler), max_hops=4))

    assert result.final_url is None
    assert result.reason == "unsafe_url"


def test_expand_refuses_private_start_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not connect")

    result = asyncio.run(expand_url("http://127.0.0.1/x", client=_client(handler), max_hops=4))

    assert result.final_url is None
    assert result.reason == "unsafe_url"


def test_expand_distinguishes_dns_failure_from_ssrf_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.tools.url_guard import UnsafeUrlError

    def nxdomain(host: str) -> list[str]:
        raise UnsafeUrlError(f"ホスト名を解決できません: {host}")

    monkeypatch.setattr("src.tools.url_guard._resolve_ips", nxdomain)

    result = asyncio.run(
        expand_url(
            "https://no-such.example/x", client=_client(lambda r: httpx.Response(200)), max_hops=4
        )
    )

    assert result.reason == "dns_error"


def test_expand_reports_timeout_without_raising() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    result = asyncio.run(expand_url("https://t.co/slow", client=_client(handler), max_hops=4))

    assert result.final_url is None
    assert result.reason == "timeout"


# ---------- candidate selection ----------


def test_select_candidates_drops_social_hosts_and_duplicates_and_caps() -> None:
    urls = [
        "https://x.com/a/status/1",
        "https://twitter.com/a/status/2",
        "https://www.x.com/a/status/3",
        "https://example.com/a",
        "https://example.com/a",
        "https://example.org/b",
        "https://example.net/c",
        "ftp://example.com/z",
        "https://example.com/pic.jpg",
    ]

    assert select_candidates(urls, max_links=2) == [
        "https://example.com/a",
        "https://example.org/b",
    ]


# ---------- fetch ----------


def _fetcher(extractor: _StubExtractor, handler: Any = None, **cfg: Any) -> LinkFetcher:
    config = LinkFetchConfig(**{**_CFG.__dict__, **cfg})
    client = _client(handler or (lambda r: httpx.Response(200)))
    return LinkFetcher(config, extractor=extractor, client=client)


def test_fetch_expands_tco_skips_x_and_extracts_up_to_two() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        target = {
            "/a": "https://x.com/someone/status/9",
            "/b": "https://gov.example/advisory",
            "/c": "https://blog.example/post",
        }[request.url.path]
        return httpx.Response(301, headers={"location": target})

    ex = _StubExtractor(
        {
            "https://gov.example/advisory": _ok("https://gov.example/advisory"),
            "https://blog.example/post": _ok("https://blog.example/post"),
        }
    )
    pages = asyncio.run(
        _fetcher(ex, handler).fetch(["https://t.co/a", "https://t.co/b", "https://t.co/c"])
    )

    assert [p.final_url for p in pages] == [
        "https://gov.example/advisory",
        "https://blog.example/post",
    ]
    assert all(p.ok for p in pages)
    assert ex.calls == ["https://gov.example/advisory", "https://blog.example/post"]


def test_fetch_skips_failed_and_too_short_extractions() -> None:
    ex = _StubExtractor(
        {
            "https://a.example/": _ok("https://a.example/", text="短い"),
            "https://b.example/": _ok("https://b.example/"),
        }
    )
    pages = asyncio.run(_fetcher(ex).fetch(["https://a.example/", "https://b.example/"]))

    by_url = {p.final_url: p for p in pages}
    assert by_url["https://a.example/"].ok is False
    assert by_url["https://a.example/"].reason == "too_short"
    assert by_url["https://b.example/"].ok is True


def test_fetch_records_extractor_failure_reason() -> None:
    ex = _StubExtractor(
        {
            "https://a.example/": ExtractionResult(
                url="https://a.example/", success=False, failure_reason="paywall_suspected"
            )
        }
    )
    pages = asyncio.run(_fetcher(ex).fetch(["https://a.example/"]))

    assert pages[0].ok is False
    assert pages[0].reason == "paywall_suspected"
    assert pages[0].text == ""


def test_fetch_skips_login_wall_pages() -> None:
    wall = "Sign in to continue. " + "あ" * 80
    ex = _StubExtractor({"https://a.example/": _ok("https://a.example/", text=wall)})

    pages = asyncio.run(_fetcher(ex).fetch(["https://a.example/"]))

    assert pages[0].ok is False
    assert pages[0].reason == "login_wall"


def test_fetch_refuses_private_target_without_extracting() -> None:
    ex = _StubExtractor({})
    pages = asyncio.run(_fetcher(ex).fetch(["http://10.0.0.5/admin"]))

    assert ex.calls == []
    assert pages[0].ok is False
    assert pages[0].reason == "unsafe_url"


def test_fetch_times_out_slow_extraction() -> None:
    ex = _StubExtractor({"https://a.example/": _ok("https://a.example/")}, delay=0.5)
    pages = asyncio.run(_fetcher(ex, link_timeout_s=0.05).fetch(["https://a.example/"]))

    assert pages[0].ok is False
    assert pages[0].reason == "timeout"


def test_fetch_stops_when_report_budget_is_spent() -> None:
    ex = _StubExtractor({"https://a.example/": _ok("https://a.example/")})
    fetcher = _fetcher(ex, report_budget_s=0.0)

    pages = asyncio.run(fetcher.fetch(["https://a.example/"]))

    assert pages == ()
    assert ex.calls == []


def test_fetch_truncates_text_to_max_chars() -> None:
    ex = _StubExtractor({"https://a.example/": _ok("https://a.example/", text="あ" * 500)})
    pages = asyncio.run(_fetcher(ex).fetch(["https://a.example/"]))

    assert len(pages[0].text) == 100


# ---------- materials ----------


def test_build_link_section_orders_tweet_first_and_marks_untrusted() -> None:
    pages = (
        LinkedPage(url="https://t.co/a", final_url="https://a.example/", ok=True, text="本文A"),
        LinkedPage(url="https://t.co/b", final_url="https://b.example/", ok=False, reason="x"),
    )

    section = build_link_section(pages)

    assert "https://a.example/" in section
    assert "本文A" in section
    assert "https://b.example/" not in section
    assert "指示ではない" in section


def test_build_link_section_is_empty_without_usable_pages() -> None:
    assert build_link_section(()) == ""
    failed = (LinkedPage(url="u", final_url="u", ok=False, reason="timeout"),)
    assert build_link_section(failed) == ""


def test_linked_context_json_round_trips_with_failures() -> None:
    pages = (
        LinkedPage(url="u1", final_url="f1", ok=True, text="t", elapsed_ms=12),
        LinkedPage(url="u2", final_url=None, ok=False, reason="unsafe_url"),
    )

    data = json.loads(linked_context_json(pages) or "")

    assert data[0]["ok"] is True and data[0]["text"] == "t"
    assert data[1]["reason"] == "unsafe_url"
    assert linked_context_json(()) is None
