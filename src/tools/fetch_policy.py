"""ソース取得の段階的エスカレーション・ポリシー (SSoT、2026-08-01)。

docs/fetch_escalation_policy.md §2。

原則: **既定は礼儀正しい bot UA で名乗り、WAF にブロックされた事実に応答して
ブラウザ相当 UA へ 1 段だけエスカレーションする**。この判断 (どの status で
エスカレーションするか / 各段のヘッダ) をここに一元化し、feed 取得・sitemap
watcher・HTML listing watcher・ソース追加/ライブプレビューの全経路が共有する。

背景 (2026-08-01 Security Joes 事案): エスカレーション実装がプレビュー経路にしか
なく、本番の毎時 fetch は bot UA 固定だったため「プレビューでは見えるのに本番は
403 で 124 回連続失敗」が起きた。**観測面 (プレビュー) と実行面 (定期取得) の取得
実装が分岐していると、観測は実行の証拠にならない** — 両者が同じ関数を通ることで
プレビューの成功 = 本番の成功可否をそのまま意味するようにする。

第 3 段 (Playwright JS レンダ) は本文抽出専用で ``content_extractor`` 側にある
(feed/sitemap が JS チャレンジを返す場合、レンダしても有効な XML は得られないため
ここには含めない)。

ネットワーク起因の失敗 (timeout / 接続断) ではエスカレーションしない — timeout の
大半は UA では回避できない tarpit で、リトライは遅延を倍にするだけ (実測知見)。
"""

from __future__ import annotations

from typing import Literal

import httpx

from src.tools.user_agent import BROWSER_HEADERS, browser_user_agent

# エスカレーションを発火させる HTTP status (WAF / bot ブロックの署名)。
# 403/429/503: 典型的な WAF ブロック応答。202: 一部 WAF のチャレンジ応答。
# 406: Accept ネゴシエーション拒否 (実測 2026-07-12 tistory 系)。
# 404/5xx (520 等) は含めない — コンテンツ/サーバ側の問題で UA では変わらない。
BLOCK_ESCALATION_STATUSES: frozenset[int] = frozenset({202, 403, 406, 429, 503})

# 各経路の Accept ヘッダ (RFC 7231 §5.3.2: 必ず */* フォールバック付き。
# 厳格な Accept のみだと 406 を返すサーバがある — 実測 blog.alyac.co.kr 158 日連続 406)
FEED_ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8"
XML_ACCEPT = "application/xml, text/xml;q=0.9, */*;q=0.8"
HTML_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

# 取得に成功した (最終応答を返した) 段。観測面はこれをユーザーに見せる:
# "browser" = bot UA がブロックされており、本番取得も同じエスカレーションで動いている
FetchStage = Literal["bot", "browser"]


def bot_headers(identity: str, *, accept: str) -> dict[str, str]:
    """第 1 段: 礼儀正しい bot 識別のヘッダ (identity は経路ごとの kuebiko/1.0 系)。"""
    return {"User-Agent": identity, "Accept": accept}


def escalated_browser_headers() -> dict[str, str]:
    """第 2 段: ブラウザ相当のヘッダ。

    UA は呼出時に解決する (``browser_user_agent`` — UA 自己修復ジョブの更新を再起動
    なしで拾う)。Accept/Accept-Language も実ブラウザ相当にする (UA 単独より通過率が
    上がる)。
    """
    return {"User-Agent": browser_user_agent(), **BROWSER_HEADERS}


def should_escalate(status: int) -> bool:
    """この status はブラウザ相当 UA で再試行する価値があるか。"""
    return status in BLOCK_ESCALATION_STATUSES


# JS チャレンジ (JS を実行しないと本文に到達できない応答) の指紋。応答ボディ先頭への
# 小文字部分一致。**status 番号でなく応答の実態で第 3 段 (Playwright) を発火させる**ため
# のもの (実測 2026-08-01: ipdefenseforum が 307 + 本文チャレンジで、block status 集合
# ベースの発火条件をすり抜けて本文抽出が 1 か月無音全滅した)。
# 固有性の高い文言のみ (汎用語は誤検出するので入れない)。
_JS_CHALLENGE_FINGERPRINTS: tuple[str, ...] = (
    "javascript is required",
    "please enable javascript",
    "enable javascript and cookies",
    "you are being redirected",
    "checking your browser",
    "just a moment",  # Cloudflare interstitial
    "challenge-platform",  # Cloudflare
    "ddos-guard",
)

# 指紋照合するボディ先頭のバイト数 (チャレンジページは小さく、先頭に文言が出る)
_JS_CHALLENGE_SCAN_CHARS = 2000


def looks_like_js_challenge(body_head: str) -> bool:
    """応答ボディが JS チャレンジページに見えるか (本文抽出の Playwright 発火判断)。"""
    blob = body_head[:_JS_CHALLENGE_SCAN_CHARS].lower()
    return any(sig in blob for sig in _JS_CHALLENGE_FINGERPRINTS)


def _pick_final(
    bot_resp: httpx.Response, browser_resp: httpx.Response | None
) -> tuple[httpx.Response, FetchStage]:
    """最終応答の選択: browser 段が成功したらそれ、駄目なら bot 段の応答を返す。

    browser 段も失敗した場合に bot 応答を返すのは、記録されるエラーを「本来の
    (エスカレーション前の) ブロック status」にするため。
    """
    if browser_resp is not None and browser_resp.status_code < 400:
        return browser_resp, "browser"
    return bot_resp, "bot"


async def staged_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    identity: str,
    accept: str,
) -> tuple[httpx.Response, FetchStage]:
    """bot UA → (ブロック時のみ) ブラウザ UA の段階取得 (async)。

    ネットワーク例外は bot 段では素通し (呼出側の既存ハンドリングへ)、
    browser 段では握って bot 応答に fallback する (エスカレーションは best-effort)。
    redirect 追従や SSRF guard は client 側の設定に従う。
    """
    bot_resp = await client.get(url, headers=bot_headers(identity, accept=accept))
    if not should_escalate(bot_resp.status_code):
        return bot_resp, "bot"
    try:
        browser_resp: httpx.Response | None = await client.get(
            url, headers=escalated_browser_headers()
        )
    except httpx.HTTPError:
        browser_resp = None
    return _pick_final(bot_resp, browser_resp)


def staged_get_sync(
    client: httpx.Client,
    url: str,
    *,
    identity: str,
    accept: str,
) -> tuple[httpx.Response, FetchStage]:
    """``staged_get`` の sync 版 (ソース追加 / ライブプレビュー経路が使う)。"""
    bot_resp = client.get(url, headers=bot_headers(identity, accept=accept))
    if not should_escalate(bot_resp.status_code):
        return bot_resp, "bot"
    try:
        browser_resp: httpx.Response | None = client.get(url, headers=escalated_browser_headers())
    except httpx.HTTPError:
        browser_resp = None
    return _pick_final(bot_resp, browser_resp)
