"""Phase F: 共通 HTTP fetch + UA retry。

Phase E-5b で feeds.py と watchers.py に重複実装した
「bot UA で 4xx → browser UA で retry」 を 1 箇所に集約。

2026-08-01: エスカーレション判断そのものを ``src/tools/fetch_policy`` (SSoT) へ移管。
本番の定期取得 (feed / sitemap / html_listing) と**同じポリシー**を通ることで、
「プレビューでは取れるのに本番は取れない」という観測と実行の乖離を構造的に防ぐ。
"""

from __future__ import annotations

from urllib.parse import urlparse

import httpx

from src.tools.fetch_policy import HTML_ACCEPT, FetchStage, staged_get_sync
from src.tools.url_guard import (
    UnsafeUrlError,
    assert_safe_public_url,
    redirect_guard_hooks,
)

DEFAULT_USER_AGENT = "kuebiko/1.0 (+source-discovery)"
DEFAULT_HTTP_TIMEOUT = 20.0
# ソース追加ウィザード等のインタラクティブ経路用 (短め)。bot 対策で tarpit される
# サイトを長時間待たず早期に fail させる。
INTERACTIVE_HTTP_TIMEOUT = 12.0


class SourceFetchError(Exception):
    """到達不可 / タイムアウト等のネットワーク起因の取得失敗。

    ``str()`` がそのまま UI 向けの分かりやすい日本語メッセージになる
    (呼び出し側は ``error=str(e)`` でユーザに提示できる)。HTTP 応答 (4xx/5xx 含む) は
    これを送出せず通常どおり status を返す。
    """


def _friendly_fetch_error(exc: httpx.HTTPError, url: str) -> str:
    """httpx のネットワーク例外を UI 向けの説明文に変換する。"""
    host = urlparse(url).hostname or url
    if isinstance(exc, httpx.TimeoutException):
        return (
            f"{host} が時間内に応答しませんでした (タイムアウト)。"
            "bot 対策で自動取得が遮断されている可能性があります。"
            "RSS フィードの URL を直接指定するか、別のソースをご検討ください。"
        )
    if isinstance(exc, httpx.ConnectError):
        return f"{host} に接続できませんでした。URL が正しいか、サイトが到達可能かご確認ください。"
    if isinstance(exc, (httpx.ReadError, httpx.RemoteProtocolError)):
        return (
            f"{host} が接続を切断しました (bot 対策の可能性)。"
            "自動取得が難しいため別のソースをご検討ください。"
        )
    return f"{host} の取得に失敗しました ({type(exc).__name__})。"


# bot 対策 / WAF ベンダの応答フィンガープリント。``(表示名, シグナル...)``。シグナルは
# 応答ヘッダ連結文字列 or ボディ (いずれも小文字) への部分一致。**上から順に評価し最初の
# 一致を採用**するため、固有のボット対策 (PerimeterX 等) を CDN 兼用の Cloudflare より前に置く
# (HUMAN は CF + PerimeterX の二重なので PerimeterX を優先したい)。誤検出回避のため固有性の
# 高い cookie 名 / ヘッダ / 文言のみを使う (汎用語は入れない)。新ベンダはこの表に 1 行足すだけ。
_BOT_VENDOR_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("PerimeterX (HUMAN)", ("px-captcha", "_pxhd", "_pxvid", "_px3", "perimeterx")),
    ("DataDome", ("x-datadome", "datadome")),
    ("Akamai Bot Manager", ("ak_bmsc", "_abck", "bm_sz", "x-akamai-")),
    ("Imperva (Incapsula)", ("incap_ses", "visid_incap", "x-iinfo", "incapsula incident")),
    ("Kasada", ("x-kpsdk", "kpsdk")),
    ("F5 / Shape", ("the requested url was rejected",)),
    ("AWS WAF", ("x-amzn-waf", "aws-waf-token")),
    ("Sucuri", ("x-sucuri-id", "sucuri website firewall", "sucuri/cloudproxy")),
    (
        "Cloudflare",
        ("cf-ray", "cf-mitigated", "__cf_bm", "cf-chl", "challenge-platform", "just a moment"),
    ),
)


def detect_bot_vendor(body: str, headers: dict[str, str]) -> str | None:
    """応答から bot 対策ベンダを検出して表示名を返す (未知 / 痕跡なしは ``None``)。"""
    blob = (body[:2000] if body else "").lower()
    hdr = " ".join(f"{k}:{v}" for k, v in headers.items()).lower()
    for name, sigs in _BOT_VENDOR_SIGNATURES:
        if any(s in hdr or s in blob for s in sigs):
            return name
    return None


def describe_blocked_status(status: int, body: str, headers: dict[str, str], url: str) -> str:
    """4xx/5xx の取得失敗を UI 向けの説明文にする。

    bot 対策 / WAF (PerimeterX / Cloudflare / Akamai / DataDome / Imperva / Kasada / F5 /
    AWS WAF / Sucuri 等) を応答フィンガープリントから検出し、ユーザに「設定ミスでなく相手が
    自動取得を拒否している」ことを明示する。未知のベンダは名前を出さず汎用文言に落とす
    (推測でベンダ名を捏造しない)。403 が cryptic に出て手詰まりに見える問題への対策。
    """
    host = urlparse(url).hostname or url
    vendor = detect_bot_vendor(body, headers)
    if status in (401, 403) and vendor:
        return (
            f"{host} はボット対策 ({vendor}) で自動取得を HTTP {status} で拒否しています。"
            "この種のサイトは RSS / HTML スクレイプとも自動収集できません "
            "(実ブラウザでもチャレンジで弾かれます)。別のソースをご検討ください。"
        )
    if status in (401, 403):
        return (
            f"{host} がアクセスを拒否しました (HTTP {status})。"
            "ログイン必須かボット対策の可能性があります。別の URL / ソースをご検討ください。"
        )
    if status == 429:
        return f"{host} がレート制限中です (HTTP 429)。時間をおいて再試行してください。"
    if status == 404:
        return f"{host} にページが見つかりません (HTTP 404)。URL をご確認ください。"
    return f"{host} の取得に失敗しました (HTTP {status})。"


def _guard_fetch_url(url: str) -> None:
    """fetch 前の SSRF 検証。private/loopback/metadata は ``SourceFetchError`` に変換。

    全 fetch がこの 1 関数を通るため、source wizard の discover / preview / sitemap など
    呼び出し側が個別にガードを書き忘れても SSRF を塞げる (security-review M3)。
    """
    try:
        assert_safe_public_url(url)
    except UnsafeUrlError as e:
        raise SourceFetchError(f"安全でない URL のため取得を中止しました: {e}") from e


def fetch_text(
    url: str, timeout: float = DEFAULT_HTTP_TIMEOUT
) -> tuple[
    int,
    str,
    dict[str, str],
    FetchStage,
]:
    """text ベースの fetch (HTML / RSS / sitemap)。 (status, body, headers, stage)。

    エスカレーションは fetch_policy (SSoT): WAF ブロック署名 status のときのみ
    browser UA で 1 回 retry。**本番の定期取得と同じ判断**なので、プレビューの成功 =
    本番の取得可否をそのまま意味する。stage は取得できた段 (bot / browser) で、
    UI が「本番も browser UA へ自動切替して取得する」ことを表示するのに使う。
    ネットワーク起因の失敗 (timeout / 接続切断等) は ``SourceFetchError`` を送出する
    (タイムアウトは browser UA でも回避できない tarpit が大半のため retry しない)。
    初回 URL と全 redirect hop を SSRF 検証する (security-review M1/M3)。
    """
    _guard_fetch_url(url)
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            event_hooks=redirect_guard_hooks(),
        ) as client:
            resp, stage = staged_get_sync(
                client, url, identity=DEFAULT_USER_AGENT, accept=HTML_ACCEPT
            )
        return resp.status_code, resp.text, dict(resp.headers), stage
    except UnsafeUrlError as e:
        raise SourceFetchError(f"安全でない URL へのリダイレクトを遮断しました: {e}") from e
    except httpx.HTTPError as e:
        raise SourceFetchError(_friendly_fetch_error(e, url)) from e


def fetch_bytes(
    url: str, timeout: float = DEFAULT_HTTP_TIMEOUT
) -> tuple[
    int,
    bytes,
    dict[str, str],
    FetchStage,
]:
    """binary fetch (XML sitemap 等で encoding を尊重したい場合)。fetch_text と同じ段階取得。"""
    _guard_fetch_url(url)
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            event_hooks=redirect_guard_hooks(),
        ) as client:
            resp, stage = staged_get_sync(
                client, url, identity=DEFAULT_USER_AGENT, accept=HTML_ACCEPT
            )
        return resp.status_code, resp.content, dict(resp.headers), stage
    except UnsafeUrlError as e:
        raise SourceFetchError(f"安全でない URL へのリダイレクトを遮断しました: {e}") from e
    except httpx.HTTPError as e:
        raise SourceFetchError(_friendly_fetch_error(e, url)) from e


def detect_kind(body: str, headers: dict[str, str]) -> str:
    """response body + content-type から kind 推定。

    Phase E-4: RDF (RSS 1.0) も RSS として扱う。
    """
    ct = headers.get("content-type", "").lower()
    body_head = body[:500].lower()
    if "rss" in ct or "rdf" in ct or "<rss" in body_head or "<rdf:rdf" in body_head:
        return "rss"
    if "atom" in ct or "<feed" in body_head:
        return "atom"
    if "sitemap" in body_head and "<urlset" in body_head:
        return "sitemap"
    if "sitemap" in body_head and "<sitemapindex" in body_head:
        return "sitemap"
    if "html" in ct or body_head.startswith("<!doctype") or "<html" in body_head:
        return "html"
    return "unknown"
