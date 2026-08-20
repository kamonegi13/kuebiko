"""本文抽出ツール (Step 5)。

Phase 1 は trafilatura ベースの Layer 1 のみ。Phase 2 で Playwright
フォールバックを追加する想定 (CLAUDE.md §6)。

設計方針:
- 例外は外に投げず、``ExtractionResult.success`` で判断する。
  上位層 (オーケストレータ) が件数集計・打ち切りを判断しやすくする。
- ``httpx.AsyncClient`` を使い回して接続プールを再利用する。複数 URL を
  並列に extract する場合に効く。
- robots.txt は **意図的に無視** している (個人利用前提)。将来公開化する
  場合の TODO は CLAUDE.md §10 で再評価する。
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlparse

import httpx
import trafilatura
from pydantic import BaseModel, ConfigDict

from src.logging_config import get_logger
from src.tools.fetch_policy import BLOCK_ESCALATION_STATUSES, looks_like_js_challenge
from src.tools.pdf_text import extract_pdf_text, looks_like_pdf
from src.tools.text_sanitizer import dominant_script, title_similarity
from src.tools.url_guard import (
    UnsafeUrlError,
    assert_safe_public_url,
    redirect_guard_hooks_async,
)
from src.tools.user_agent import (
    BROWSER_HEADERS,
    DEFAULT_CHROME_MAJOR,
    browser_user_agent,
)

_log = get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0

# ドメイン別の本文コンテナ XPath (pre-trim)。サイト改装で trafilatura が本文ノードの選択を
# 誤る場合に、抽出入力をこのサブツリーへ絞る。**セレクタ不一致・パース失敗は全文のまま
# fail-open** (汎用抽出へ)。metadata/言語/paywall 判定は常に全文 HTML を使う (head を失わない)。
# 2026-08-21: The Register 改装 (teaser <article> 多数 + toplist 列) で本文でなくナビ列が
# 約 2 か月抽出されていた (全記事が同一 2,494 字の MOST POPULAR 列)。実本文は k5a-article。
_MAIN_CONTENT_XPATH_BY_HOST: dict[str, str] = {
    "theregister.com": "//*[contains(@class, 'k5a-article')]",
}

# 本文取得の UA 戦略 (2026-07-27, docs/body_extraction_and_entity_integrity_redesign.md §2.2)。
# 陳腐化した UA (旧 Chrome/120) そのものが WAF のボット署名になり全文取得が 403 で無音失敗して
# いた (GBHackers 953/953=100% 切り株)。UA は src/tools/user_agent.py の単一ソースに集約し、
# 全 fetch 経路 (本文抽出 / ソース追加プレビュー / JS watcher) が共有する。ここでは 403/429/503
# の代替 UA ローテ (プライマリの Chrome メジャー版から導出) を担う。
_BROWSER_HEADERS = BROWSER_HEADERS  # 後方互換 alias
_resolve_default_user_agent = browser_user_agent  # 後方互換 alias (既存 import 参照用)
_CHROME_VERSION_RE = re.compile(r"Chrome/(\d+)")


def _rotation_uas(primary_ua: str) -> tuple[str, ...]:
    """403/429/503 で順に試す代替 UA を **プライマリの Chrome メジャー版から導出**する。

    自己修復ジョブがプライマリ UA を更新すると代替もそれに追従する (プール全体が単一の
    バージョン源から現行に保たれる、2026-07-27)。プライマリが Chrome 形式でなければ
    デフォルト版で組む。
    """
    m = _CHROME_VERSION_RE.search(primary_ua)
    major = int(m.group(1)) if m else DEFAULT_CHROME_MAJOR
    return (
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36",
        f"Mozilla/5.0 (Macintosh; Intel Mac OS X 14.7; rv:{major}.0) "
        f"Gecko/20100101 Firefox/{major}.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/18.2 Safari/605.1.15",
    )


# UA rotation retry を発火させる HTTP status (WAF/bot ブロック系)。
# SSoT は fetch_policy (2026-08-01 統一 — 本文抽出だけ 406 を欠くといった分岐を作らない)
_RETRIABLE_BLOCK_STATUSES: frozenset[int] = BLOCK_ESCALATION_STATUSES

# Playwright 試行の per-run 既定 cap。1 記事 10-30 秒 × cap ≈ 最大 5 分で
# pipeline 時間予算 (soft deadline) と両立する水準。env で調整可、0 以下 = 無制限。
_PLAYWRIGHT_CAP_DEFAULT = 10


def _playwright_cap_from_env() -> int:
    """Playwright 試行 cap を解決する (env PLAYWRIGHT_EXTRACT_CAP → 既定値)。"""
    raw = os.environ.get("PLAYWRIGHT_EXTRACT_CAP", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            _log.warning("playwright_cap_invalid", value=raw)
    return _PLAYWRIGHT_CAP_DEFAULT


# 後方互換: 既存の import 参照用 (module load 時に env を反映)。
DEFAULT_USER_AGENT = browser_user_agent()
DEFAULT_MIN_CONTENT_LENGTH = 200

# 短文ページ受理の下限 (min_content_length に対する比率、2026-08-01)。
# 公式 advisory 型の「短いが完全なページ」を一律閾値で捨てないための汎用ルール。
# 0.7 の根拠: JVN 型 (189 字 / min 200) を救い、明白な断片 (数十字) は弾く水準。
_SHORT_PAGE_ACCEPT_RATIO = 0.7

# Cloudflare / bot 検知 を回避する init script (src/watchers/playwright_base.py と同じ)。
# navigator.webdriver の隠蔽 + plugins の偽装でヘッドレス検知を回避。
_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'languages', {
    get: () => ['en-US', 'en', 'ja-JP', 'ja'],
});
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
"""

# ペイウォール疑いキーワード (英語 + 日本語)。
# 抽出本文または HTML 全体に含まれていれば疑わしいと判定する。
PAYWALL_KEYWORDS: tuple[str, ...] = (
    "subscribe to read",
    "subscribe to continue",
    "subscriber-only",
    "paywall",
    "sign in to continue",
    "log in to read",
    "subscription required",
    "登録して続きを読む",
    "ログインして続きを読む",
    "購読",
    "会員限定",
    "有料会員",
    "続きをお読みいただくには",
)

EXTRACTION_METHOD_TRAFILATURA = "trafilatura"
# PDF でしか勧告を出さない一次ソース (BSI / CSA 等) 用 (2026-08-15)
EXTRACTION_METHOD_PDF = "pdf"


class ExtractionResult(BaseModel):
    """本文抽出の結果 (成功・失敗の両方を表現する)。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    url: str
    title: str | None = None
    author: str | None = None
    published_date: datetime | None = None
    text: str = ""
    language: str | None = None
    success: bool
    failure_reason: str | None = None
    extraction_method: str = EXTRACTION_METHOD_TRAFILATURA


class ContentExtractor:
    """trafilatura で記事 URL から本文を抽出する非同期クライアント。

    使い方::

        async with ContentExtractor() as ex:
            result = await ex.extract("https://example.com/article")
            if result.success:
                print(result.text)
            else:
                print(f"failed: {result.failure_reason}")
    """

    def __init__(
        self,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        user_agent: str | None = None,
        min_content_length: int = DEFAULT_MIN_CONTENT_LENGTH,
        client: httpx.AsyncClient | None = None,
        *,
        enable_playwright_fallback: bool = True,
        playwright_navigation_timeout_ms: int = 30_000,
        playwright_post_load_wait_ms: int = 5_000,
        playwright_attempt_cap: int | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        # UA は **構築時**に env から解決する (module load 時の 1 回束縛だと自己修復ジョブの
        # .env 更新が再起動まで効かないため、2026-07-27)。明示指定時はそれを優先 (テスト用)。
        self._user_agent = user_agent or _resolve_default_user_agent()
        self._min_content_length = min_content_length
        if client is None:
            self._client = httpx.AsyncClient(
                timeout=timeout_seconds,
                headers={"User-Agent": self._user_agent, **_BROWSER_HEADERS},
                follow_redirects=True,
                event_hooks=redirect_guard_hooks_async(),
            )
            self._owns_client = True
        else:
            self._client = client
            self._owns_client = False
        # Phase Diamond L2: Playwright fallback (Cloudflare bot block / JS challenge 突破)
        self._enable_playwright = enable_playwright_fallback
        self._playwright_nav_timeout_ms = playwright_navigation_timeout_ms
        self._playwright_post_load_wait_ms = playwright_post_load_wait_ms
        # per-run cap (2026-08-01): Playwright は 1 記事 10-30 秒かかるため、extractor
        # インスタンス (= 1 run) あたりの試行回数を抑えて時間予算と干渉させない。
        # cap 超過分は既存の failure 経路に落ち、refetch backlog が次周期で拾う。
        self._playwright_cap = (
            playwright_attempt_cap
            if playwright_attempt_cap is not None
            else _playwright_cap_from_env()
        )
        self._playwright_attempts = 0
        # Lazy-init: 初回 fallback 呼出時に起動 (trafilatura で全 OK なら起動不要)
        self._playwright_obj: object | None = None
        self._playwright_browser: object | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
        # Playwright 起動済なら shutdown
        if self._playwright_browser is not None:
            try:
                await self._playwright_browser.close()  # type: ignore[attr-defined]
            except Exception as e:  # noqa: BLE001
                _log.debug("playwright_browser_close_failed", error=str(e))
        if self._playwright_obj is not None:
            try:
                await self._playwright_obj.stop()  # type: ignore[attr-defined]
            except Exception as e:  # noqa: BLE001
                _log.debug("playwright_stop_failed", error=str(e))

    async def extract(self, url: str) -> ExtractionResult:
        """URL から本文を取得・抽出する。例外は投げず ``ExtractionResult`` を返す。"""
        _log.info("content_extract_start", url=url)

        # 0. SSRF 検証: 記事 URL は feed の <link> 由来 = 攻撃者制御可能。private/loopback/
        #    metadata を指す URL や、そこへ 30x 誘導する応答 (redirect hook) を遮断する (M1)。
        try:
            assert_safe_public_url(url)
        except UnsafeUrlError as e:
            return self._fail(url, "unsafe_url", error=str(e))

        # 1. fetch (403/429/503 は代替 UA で再試行 = WAF の UA fingerprint ブロック救済)
        try:
            resp = await self._get_with_ua_rotation(url)
        except httpx.TimeoutException as e:
            return self._fail(url, "timeout", error=str(e))
        except UnsafeUrlError as e:
            return self._fail(url, "unsafe_redirect", error=str(e))
        except httpx.RequestError as e:
            return self._fail(url, "connection_error", error=str(e))

        if resp.status_code != 200:
            # Phase Diamond L2: Cloudflare bot block (403) / JS challenge / async response
            # (202) は Playwright fallback で突破を試みる。
            # 2026-08-01: status 集合に加え **応答の実態 (JS チャレンジ指紋)** でも発火する。
            # ipdefenseforum は 307 + チャレンジ本文で status ベースの条件をすり抜け、
            # Playwright 経路が一度も発火せず本文抽出が 1 か月無音全滅した。
            is_challenge = looks_like_js_challenge(resp.text)
            if resp.status_code in _RETRIABLE_BLOCK_STATUSES or is_challenge:
                pw_result = await self._playwright_within_cap(url)
                if pw_result is not None:
                    return pw_result
            if is_challenge:
                # UA では直らない失敗として区別する (購読ソース画面の triage 材料)
                return self._fail(url, "js_challenge", status=resp.status_code)
            return self._fail(
                url,
                f"http_error_{resp.status_code}",
                status=resp.status_code,
            )

        # 1.5 PDF はここで分岐する。trafilatura は HTML 専用で、PDF を渡すと本文 0 文字
        #     → extract_failed で終端し、要約も重要度判定もされないまま配信されない
        #     (BSI Cybersicherheitswarnungen が実際にこの状態だった)。
        if looks_like_pdf(content_type=resp.headers.get("content-type", ""), body=resp.content):
            return await self._extract_pdf(url, resp.content)

        html = resp.text

        # 2. extract body text
        # ``deduplicate=False``: trafilatura の dedup は呼び出し間で状態を持ち、
        # 同一/類似 HTML を後続の extract で None にしてしまう。我々のユースケース
        # (URL ごとに独立な記事) では cross-call dedup は害なので無効化する。
        try:
            text = trafilatura.extract(
                pretrim_main_content(url, html),
                include_comments=False,
                include_tables=False,
                favor_recall=False,
                deduplicate=False,
            )
        except Exception as e:  # noqa: BLE001  # trafilatura の例外型は不安定
            text = None
            _log.debug("trafilatura_extract_failed", url=url, error=str(e))

        if not text or len(text) < self._min_content_length:
            # trafilatura で取れなかった / 短すぎる → Playwright fallback
            # (Cloudflare interstitial HTML や JS-rendered content への対応)
            pw_result = await self._playwright_within_cap(url)
            if pw_result is not None:
                return pw_result
            if not text:
                return self._fail(url, "extraction_failed", note="trafilatura returned None")
            if _looks_like_paywall(text=text, html=html):
                return self._fail(
                    url,
                    "paywall_suspected",
                    length=len(text),
                )
            # 短文ページの汎用受理 (2026-08-01): 公式 advisory (JVN 等) は「概要 + 詳細表」
            # の構造でページ自体が短く、抽出は成功しているのに一律閾値で捨てられていた
            # (実測: JVNVU が 189 字で min 200 に 11 字不足 → extract_failed 10 件/30日)。
            # 閾値の一定割合以上あれば「短いが完全なページ」として採用する。paywall 判定を
            # 通過済み + Playwright でも増えなかった後なので、断片の誤採用リスクは小さい。
            if len(text) >= int(self._min_content_length * _SHORT_PAGE_ACCEPT_RATIO):
                _log.info("content_extract_short_page_accepted", url=url, length=len(text))
            else:
                return self._fail(
                    url,
                    "content_too_short",
                    length=len(text),
                )

        # 4. metadata extraction (best-effort)
        metadata = _safe_extract_metadata(html)
        result = ExtractionResult(
            url=url,
            title=_get_attr(metadata, "title"),
            author=_get_attr(metadata, "author"),
            published_date=_parse_iso_date(_get_attr(metadata, "date")),
            text=text,
            language=_get_attr(metadata, "language") or _extract_html_lang(html),
            success=True,
            failure_reason=None,
            extraction_method=EXTRACTION_METHOD_TRAFILATURA,
        )
        _log.info(
            "content_extract_success",
            url=url,
            length=len(text),
            language=result.language,
        )
        return result

    async def _get_with_ua_rotation(self, url: str) -> httpx.Response:
        """URL を GET。403/429/503 なら代替 UA (現行 Chrome/Firefox/Safari) で順に再試行する。

        WAF は UA バージョンの fingerprint に反応してブロックすることがある (実測: Chrome/120=403、
        Chrome/126=200)。プライマリ UA で block されたら代替 UA を試し、最後の応答を返す。
        200 が得られた時点で即座に返す。例外 (timeout/connection) は呼出側で捕捉する。
        """
        resp = await self._client.get(url)
        if resp.status_code not in _RETRIABLE_BLOCK_STATUSES:
            return resp
        for alt_ua in _rotation_uas(self._user_agent):
            if alt_ua == self._user_agent:
                continue
            _log.info(
                "content_extract_ua_retry",
                url=url,
                blocked_status=resp.status_code,
                retry_ua=alt_ua.split(") ", 1)[-1][:24],
            )
            try:
                retry = await self._client.get(url, headers={"User-Agent": alt_ua})
            except (httpx.TimeoutException, httpx.RequestError):
                continue
            if retry.status_code == 200:
                return retry
            resp = retry
        return resp

    # --- Playwright fallback (Phase Diamond L2) ---

    async def _ensure_playwright(self) -> bool:
        """Playwright browser を lazy-init。失敗時 False を返して fallback を諦める。"""
        if self._playwright_browser is not None:
            return True
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            _log.debug("playwright_not_installed")
            return False
        try:
            self._playwright_obj = await async_playwright().start()
            self._playwright_browser = await self._playwright_obj.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--no-default-browser-check",
                    "--no-first-run",
                ],
                ignore_default_args=["--enable-automation"],
            )
            _log.info("playwright_browser_started")
            return True
        except Exception as e:  # noqa: BLE001
            _log.warning("playwright_browser_start_failed", error=str(e))
            return False

    async def _playwright_within_cap(self, url: str) -> ExtractionResult | None:
        """Playwright fallback を per-run cap の範囲内で試行する。

        cap 到達後は None (呼出側が通常の failure 経路へ)。cap は成功/失敗を問わず
        試行で消費する — 1 記事 10-30 秒の重い段が時間予算を食い潰さないための上限で、
        溢れた分は refetch backlog が次周期で拾う。
        """
        if not self._enable_playwright:
            return None
        if self._playwright_cap > 0 and self._playwright_attempts >= self._playwright_cap:
            _log.info("playwright_fallback_cap_reached", url=url, cap=self._playwright_cap)
            return None
        self._playwright_attempts += 1
        return await self._extract_with_playwright(url)

    async def _extract_with_playwright(self, url: str) -> ExtractionResult | None:
        """Playwright で URL を fetch → HTML 取得 → trafilatura で抽出。

        Cloudflare bot block / JS challenge / 202 async response を突破する目的。
        失敗時 None (呼出側が再 fail() で適切な reason を返す)。
        """
        if not await self._ensure_playwright():
            return None

        from playwright.async_api import (
            TimeoutError as PlaywrightTimeoutError,
        )

        browser = self._playwright_browser
        if browser is None:
            return None
        context = None
        try:
            context = await browser.new_context(  # type: ignore[attr-defined]
                user_agent=self._user_agent,
                viewport={"width": 1280, "height": 800},
                locale="en-US",
            )
            context.set_default_navigation_timeout(self._playwright_nav_timeout_ms)
            await context.add_init_script(_STEALTH_INIT_SCRIPT)
            page = await context.new_page()
            _log.info("playwright_fetch_start", url=url)
            try:
                response = await page.goto(url, wait_until="domcontentloaded")
            except PlaywrightTimeoutError as e:
                _log.info("playwright_fetch_timeout", url=url, error=str(e))
                return None
            # Cloudflare interstitial 用に少し待つ (JS challenge が解決されるまで)
            if self._playwright_post_load_wait_ms > 0:
                await page.wait_for_timeout(self._playwright_post_load_wait_ms)
            html = await page.content()
            status = response.status if response is not None else 0
        except Exception as e:  # noqa: BLE001
            _log.info("playwright_fetch_error", url=url, error=str(e))
            return None
        finally:
            if context is not None:
                try:
                    await context.close()
                except Exception as e:  # noqa: BLE001
                    _log.debug("playwright_context_close_failed", error=str(e))

        if not html:
            return None

        # trafilatura で text 抽出 (httpx 経路と同じドメイン別 pre-trim を適用)
        try:
            text = trafilatura.extract(
                pretrim_main_content(url, html),
                include_comments=False,
                include_tables=False,
                favor_recall=False,
                deduplicate=False,
            )
        except Exception as e:  # noqa: BLE001
            _log.debug("playwright_trafilatura_failed", url=url, error=str(e))
            return None

        if not text or len(text) < self._min_content_length:
            _log.info(
                "playwright_extract_too_short",
                url=url,
                length=len(text) if text else 0,
                http_status=status,
            )
            return None

        # paywall 判定
        if _looks_like_paywall(text=text, html=html):
            _log.info("playwright_paywall_suspected", url=url, length=len(text))
            return None

        metadata = _safe_extract_metadata(html)
        result = ExtractionResult(
            url=url,
            title=_get_attr(metadata, "title"),
            author=_get_attr(metadata, "author"),
            published_date=_parse_iso_date(_get_attr(metadata, "date")),
            text=text,
            language=_get_attr(metadata, "language") or _extract_html_lang(html),
            success=True,
            failure_reason=None,
            extraction_method="playwright+trafilatura",
        )
        _log.info(
            "playwright_extract_success",
            url=url,
            length=len(text),
            http_status=status,
        )
        return result

    # --- internal helpers ---

    async def _extract_pdf(self, url: str, body: bytes) -> ExtractionResult:
        """PDF 本文を抽出する (テキスト PDF のみ。スキャン画像は OCR しない)。

        pypdf は CPU 同期処理なので、他ソースの並列取得を止めないよう thread に逃がす。
        """
        text = await asyncio.to_thread(extract_pdf_text, body)
        if not text:
            # 空 = スキャン画像 / 暗号化 / 破損。UA 再試行でも直らないので区別して記録する。
            return self._fail(url, "pdf_no_text", size=len(body))
        if len(text) < self._min_content_length:
            return self._fail(url, "content_too_short", length=len(text))
        _log.info("content_extract_success", url=url, length=len(text), method="pdf")
        return ExtractionResult(
            url=url,
            text=text,
            success=True,
            failure_reason=None,
            extraction_method=EXTRACTION_METHOD_PDF,
        )

    def _fail(self, url: str, reason: str, **extra: Any) -> ExtractionResult:
        _log.info("content_extract_failed", url=url, reason=reason, **extra)
        return ExtractionResult(
            url=url,
            success=False,
            failure_reason=reason,
            extraction_method=EXTRACTION_METHOD_TRAFILATURA,
        )


# ---------- module-level helpers ----------


def pretrim_main_content(url: str, html: str) -> str:
    """ドメイン別 XPath で本文コンテナへ pre-trim する (不一致・失敗は全文のまま)。

    trafilatura への **入力専用**。metadata 抽出・言語判定・paywall 判定には使わない
    (head / 全文の情報を失うため)。対象ドメインは _MAIN_CONTENT_XPATH_BY_HOST。
    """
    host = (urlparse(url).hostname or "").lower()
    xpath = next(
        (
            xp
            for h, xp in _MAIN_CONTENT_XPATH_BY_HOST.items()
            if host == h or host.endswith("." + h)
        ),
        None,
    )
    if xpath is None:
        return html
    try:
        from lxml import html as lxml_html

        raw = lxml_html.fromstring(html).xpath(xpath)
        # XPath の戻りは式次第で bool/float/str にもなる — 要素ノードのみに絞る
        nodes = (
            [n for n in raw if isinstance(n, lxml_html.HtmlElement)]
            if isinstance(raw, list)
            else []
        )
        if not nodes:
            _log.info("content_pretrim_selector_missed", url=url)
            return html
        sub = lxml_html.tostring(nodes[0], encoding="unicode")
    except Exception as e:  # noqa: BLE001 — pre-trim は最適化であり失敗を致命化しない
        _log.debug("content_pretrim_failed", url=url, error=str(e))
        return html
    _log.info("content_pretrim_applied", url=url, trimmed_length=len(sub))
    return f"<html><body>{sub}</body></html>"


def _looks_like_paywall(*, text: str, html: str) -> bool:
    """抽出本文 / HTML のいずれかにペイウォールキーワードが含まれるか。"""
    text_lower = text.lower()
    html_lower = html.lower()
    return any(kw in text_lower or kw in html_lower for kw in PAYWALL_KEYWORDS)


def _safe_extract_metadata(html: str) -> object | None:
    try:
        return trafilatura.extract_metadata(html)
    except Exception:  # noqa: BLE001
        return None


def _get_attr(obj: object | None, name: str) -> str | None:
    if obj is None:
        return None
    value = getattr(obj, name, None)
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


_HTML_LANG_PATTERN = re.compile(
    r'<html[^>]*\blang\s*=\s*["\']([^"\'>]+)["\']',
    re.IGNORECASE,
)


def _extract_html_lang(html: str) -> str | None:
    """``<html lang="en-US">`` のような属性から言語コードを取り出す。"""
    match = _HTML_LANG_PATTERN.search(html)
    if not match:
        return None
    lang = match.group(1).strip()
    if not lang:
        return None
    # "en-US" -> "en", "ja-JP" -> "ja"
    return lang.split("-", 1)[0].lower()


def _parse_iso_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    cleaned = date_str.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


# 抽出本文と記事の同一性の下限 (2026-08-18)。**まだ棄却はしない — 観測のみ**。
# 実測 (14 日 1,615 件) では別記事混入は 7 件 (0.4%) で、正解は 1.0 / 誤りは 0.0 と
# はっきり分かれた。ただし「サイト側タイトルが取れない」ケースの分布が未知なので、
# 本番ログを 1 日集めてから棄却に切り替える (勘で閾値を決めて良い記事を捨てない)。
MIN_TITLE_SIMILARITY = 0.30


def check_extracted_identity(
    result: ExtractionResult,
    expected_title: str | None,
    *,
    article_id: str = "",
) -> float | None:
    """抽出本文が **当該記事のものか** を測って記録する。``None`` = 判定材料なし。

    取得の成否とは別の軸。fetch は 200 でも中身が別記事のことがあり
    (databreachtoday の npm 記事に別記事の本文、The Register にナビ断片)、
    「取得成功 N 件」しか見ていなかったため 2 か月検知できなかった。
    """
    if not result.success or not result.title or not expected_title:
        return None
    # 文字種が違うタイトル対 (韓国語ページ vs 日本語 RSS 等) は n-gram では常に 0 点で
    # 「別記事混入」と区別できない — 初日実測で警告の大半がこの型の誤検知だった。
    # 測れないものは「判定材料なし」に倒す (0 点の偽警告で真の混入を埋もれさせない)。
    if dominant_script(result.title) != dominant_script(expected_title):
        return None
    similarity = title_similarity(result.title, expected_title)
    if similarity < MIN_TITLE_SIMILARITY:
        # 目視検証のため両タイトルを残す (初日は数値のみで毎回 DB 遡及が必要だった)。
        # タイトルは配信文面に既に出る情報で §4 の機密には当たらない。80 字で切る。
        _log.warning(
            "extracted_body_title_mismatch",
            article_id=article_id,
            url=result.url,
            similarity=round(similarity, 3),
            extracted_title=result.title[:80],
            expected_title=expected_title[:80],
        )
    return similarity
