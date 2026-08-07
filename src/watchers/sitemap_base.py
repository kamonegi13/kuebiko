"""sitemap.xml ベースの URL discovery → ``Article`` emit を行う共通基盤 (Phase 5T-B)。

設計動機:
    Phase 5R で導入した直接 HTML scrape 方式 (project_zero, lac_watch) は
    各 site で HTML 構造を個別解析する必要があり工数が大きい。一方で多くの
    think tank / 政府 CERT / 研究機関は ``sitemap.xml`` を公開しており、
    そこから article URL を取り出すだけで OK な場合が多い (本文取得は後段
    パイプラインの trafilatura に委譲)。

設計方針:
    - 各 watcher 用 module (例: ``ccdcoe.py``) は ``SitemapWatcher`` を
      インスタンス化するだけ。HTML パースのコードは持たない
    - ``sitemap_urls`` には index ではなく **最終的な urlset XML** を直接
      指定する (sub-sitemap 取得は呼び出し側が事前に確定)
    - URL の filter は include regex + exclude regex で per-site 制御
    - seen 状態は ``data/<name>_seen.json`` に保存 (project_zero と同パターン)
    - ``fetch_articles()`` は ``Article`` (id/url/title placeholder) を返す。
      本文と公開日は後段 trafilatura が URL から取得する

公開 API:
    - ``SitemapWatcher(name, sitemap_urls, url_include_regex, ...).fetch_articles()``
    - ``init_seen()``: 初回 false-positive 防止のため現状の全 URL を seen 化
    - ``read_state()``: 状態スナップショット (UI 用)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict

from src.logging_config import get_logger
from src.tools.article_model import Article
from src.tools.fetch_policy import XML_ACCEPT, staged_get
from src.tools.source_fetch_outcome import FetchObservation, SourceFetchOutcome
from src.tools.url_guard import (
    UnsafeUrlError,
    assert_safe_public_url,
    redirect_guard_hooks_async,
)

_log = get_logger(__name__)

DEFAULT_USER_AGENT = "kuebiko/1.0 (+sitemap-watcher)"
DEFAULT_HTTP_TIMEOUT = 30.0
DEFAULT_MAX_POSTS_PER_RUN = 10

# Sitemap 0.9 spec は http:// namespace を正規としているが、
# 一部サイト (IPA 等) は https:// 版で宣言しているため、
# namespace 不問の {*}tag ワイルドカードで照合する。


def _findall_localname(root: ET.Element, path: list[str]) -> list[ET.Element]:
    """Namespace-agnostic findall。`path` は local name の列。

    例: _findall_localname(root, ["url", "loc"]) は ``.//{*}url/{*}loc`` 相当。
    ElementTree は 3.10+ で ``{*}tag`` ワイルドカード対応のため利用する。
    """
    xpath = ".//" + "/".join(f"{{*}}{p}" for p in path)
    return list(root.findall(xpath))


class WatcherState(BaseModel):
    """UI 表示用 watcher 状態スナップショット (Project Zero / LAC Watch と互換)。"""

    model_config = ConfigDict(frozen=True)

    name: str
    seen_count: int
    state_file_exists: bool
    last_modified: datetime | None
    state_path: str


@dataclass(frozen=True)
class SitemapWatcher:
    """sitemap.xml ベースの汎用 watcher。

    Attributes:
        name: watcher 識別子 (例: "ccdcoe"、URL safe な kebab-case)
        sitemap_urls: 取得する sitemap URL のリスト (urlset 形式を期待)
        url_include_pattern: 含めたい記事 URL の正規表現 (例: r"/news/2026/")
        url_exclude_pattern: 除外する URL の正規表現 (オプション)
        state_file: seen URL を保存する JSON ファイルパス
        feed_title: Article.feed_title に入る表示用名前
        max_posts_per_run: 1 回の fetch で返す article 数の上限
        user_agent: HTTP リクエストの User-Agent
    """

    name: str
    sitemap_urls: tuple[str, ...]
    url_include_pattern: re.Pattern[str]
    state_file: Path
    feed_title: str
    url_exclude_pattern: re.Pattern[str] | None = None
    max_posts_per_run: int = DEFAULT_MAX_POSTS_PER_RUN
    user_agent: str = DEFAULT_USER_AGENT
    http_timeout: float = DEFAULT_HTTP_TIMEOUT
    # 死活観測 (2026-08-02)。frozen dataclass だが中身は可変ホルダなので記録できる。
    # 詳細と設計理由は src/tools/source_fetch_outcome.py を参照。
    observation: FetchObservation = field(
        default_factory=FetchObservation, compare=False, repr=False
    )

    def last_fetch_outcome(self) -> SourceFetchOutcome | None:
        """直近取得の死活 (未取得なら None)。結合キーは購読一覧と同じ先頭 sitemap URL。"""
        key = self.sitemap_urls[0] if self.sitemap_urls else self.name
        return self.observation.to_outcome(key, self.feed_title or self.name)

    # ---- URL 取得 ----

    async def _fetch_sitemap(self, client: httpx.AsyncClient, url: str) -> list[str]:
        """1 つの sitemap URL から ``<url><loc>`` の値を取り出す。

        sitemap index (``<sitemapindex>``) が返された場合は 1 階層だけ再帰して
        sub-sitemap の URL も収集する (深さ 2 まで)。
        """
        try:
            assert_safe_public_url(url)
        except UnsafeUrlError as e:
            _log.warning("sitemap_unsafe_url", watcher=self.name, url=url, error=str(e))
            return []
        try:
            # fetch_policy: bot UA → WAF ブロック時のみブラウザ UA (プレビューと同一ポリシー)
            resp, _stage = await staged_get(
                client, url, identity=self.user_agent, accept=XML_ACCEPT
            )
        except (httpx.HTTPError, UnsafeUrlError) as e:
            _log.warning("sitemap_fetch_failed", watcher=self.name, url=url, error=str(e))
            return []
        text = resp.text
        if resp.status_code >= 400:
            # 第3段 (監査 2026-08-01 追補): UA 2 段でも解けない JS チャレンジ (ISW 型の
            # Cloudflare 恒久 403) は Playwright で突破する — 本文取得層と同じ最終段を
            # listing 層にも。発火はブロック署名 + チャレンジ指紋のときのみ (希少)。
            from src.tools.fetch_policy import looks_like_js_challenge, should_escalate
            from src.tools.playwright_fetch import fetch_text_via_playwright

            pw_text: str | None = None
            if should_escalate(resp.status_code) and looks_like_js_challenge(text):
                pw_text = await fetch_text_via_playwright(url, user_agent=self.user_agent)
            if pw_text:
                _log.info("sitemap_playwright_recovered", watcher=self.name, url=url)
                text = pw_text
            else:
                _log.warning(
                    "sitemap_fetch_failed",
                    watcher=self.name,
                    url=url,
                    error=f"HTTP {resp.status_code}",
                )
                return []

        try:
            root = ET.fromstring(text)
        except ET.ParseError as e:
            _log.warning("sitemap_parse_failed", watcher=self.name, url=url, error=str(e))
            return []

        tag = root.tag
        # XML namespace を除去した tag 名
        local = tag.split("}", 1)[-1] if "}" in tag else tag

        if local == "sitemapindex":
            # sub-sitemap を 1 階層 follow
            sub_urls = [
                loc.text or "" for loc in _findall_localname(root, ["sitemap", "loc"]) if loc.text
            ]
            results: list[str] = []
            for sub_url in sub_urls:
                # 再帰呼び出しは 1 階層のみ (深さ制御のため局所処理)。
                # sub_url は sitemap 本文由来 = 攻撃者制御可能なので fetch 前に SSRF 検証する
                # (compromised sitemap が host.docker.internal 等を注入する経路を塞ぐ。M2)。
                try:
                    assert_safe_public_url(sub_url)
                    sub_resp = await client.get(sub_url)
                    sub_resp.raise_for_status()
                    sub_root = ET.fromstring(sub_resp.text)
                    results.extend(
                        loc.text or ""
                        for loc in _findall_localname(sub_root, ["url", "loc"])
                        if loc.text
                    )
                except (httpx.HTTPError, ET.ParseError, UnsafeUrlError) as e:
                    _log.warning(
                        "sitemap_sub_fetch_failed",
                        watcher=self.name,
                        url=sub_url,
                        error=str(e),
                    )
            return results

        if local == "urlset":
            return [loc.text or "" for loc in _findall_localname(root, ["url", "loc"]) if loc.text]

        _log.warning("sitemap_unknown_root", watcher=self.name, tag=tag)
        return []

    async def _collect_all_urls(self) -> list[str]:
        """全 sitemap_urls から URL を集約 (重複排除済)。"""
        seen_dedup: set[str] = set()
        out: list[str] = []
        # UA ヘッダは fetch_policy が per-request で組む (client 既定 UA は持たない)
        async with httpx.AsyncClient(
            timeout=self.http_timeout,
            follow_redirects=True,
            event_hooks=redirect_guard_hooks_async(),
        ) as client:
            results = await asyncio.gather(
                *(self._fetch_sitemap(client, url) for url in self.sitemap_urls),
                return_exceptions=True,
            )
        for result in results:
            if isinstance(result, BaseException):
                continue
            for url in result:
                if url not in seen_dedup:
                    seen_dedup.add(url)
                    out.append(url)
        return out

    def _filter_urls(self, urls: list[str]) -> list[str]:
        """include / exclude regex で URL を篩う。"""
        out: list[str] = []
        for u in urls:
            if not self.url_include_pattern.search(u):
                continue
            if self.url_exclude_pattern and self.url_exclude_pattern.search(u):
                continue
            out.append(u)
        return out

    # ---- seen state ----

    def _load_seen(self) -> set[str]:
        if not self.state_file.exists():
            return set()
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            return set(data.get("seen_urls", []))
        except (json.JSONDecodeError, OSError) as e:
            _log.warning("seen_load_failed", watcher=self.name, error=str(e))
            return set()

    def _save_seen(self, urls: set[str]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"seen_urls": sorted(urls)}
        tmp = self.state_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_file)

    # ---- Article 変換 ----

    def _url_to_article(self, url: str) -> Article:
        """URL を ``Article`` に変換。本文と公開日は後段 trafilatura が取得する。"""
        article_id = f"{self.name}:{hashlib.sha1(url.encode()).hexdigest()[:16]}"
        # 暫定 title は URL の最後の slug
        slug = url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").replace("_", " ")
        title = slug[:200] if slug else url
        # 公開日は後段で trafilatura が取得し直すので暫定で current time
        published = datetime.now().astimezone()
        return Article(
            id=article_id,
            title=title,
            url=url,
            summary_html="",
            author=None,
            published=published,
            feed_title=self.feed_title,
            feed_url=self.sitemap_urls[0] if self.sitemap_urls else "",
        )

    # ---- 公開 API ----

    async def fetch_articles(self, max_count: int | None = None) -> list[Article]:
        """新規 URL を ``Article`` として返す。seen は fetch 時に更新する。

        Args:
            max_count: 上限 (default は ``max_posts_per_run``)
        """
        cap = min(max_count or self.max_posts_per_run, self.max_posts_per_run)

        all_urls = await self._collect_all_urls()
        if not all_urls:
            _log.warning("sitemap_no_urls", watcher=self.name)
            self.observation.record_failure("sitemap を取得できないか URL が 0 件です")
            return []

        filtered = self._filter_urls(all_urls)
        if not filtered:
            # sitemap は取れているのに include/exclude に 1 件も一致しない = パターン
            # 陳腐化 (年別パスの年跨ぎ / サイト構造変更)。無音で死ぬ経路なので失敗扱い。
            self.observation.record_failure(
                f"sitemap は取得できましたが URL パターンに一致しません "
                f"(取得 {len(all_urls)} 件・一致 0 件。サイト構造変更の可能性)"
            )
            return []
        self.observation.record_success(len(filtered))
        seen = self._load_seen()
        new_urls = [u for u in filtered if u not in seen]

        _log.info(
            "sitemap_fetch",
            watcher=self.name,
            total=len(all_urls),
            filtered=len(filtered),
            seen=len(seen),
            new=len(new_urls),
        )

        if not new_urls:
            return []

        if len(new_urls) > cap:
            _log.warning(
                "sitemap_too_many_new_capping",
                watcher=self.name,
                new=len(new_urls),
                cap=cap,
            )
            new_urls = new_urls[:cap]

        # seen 更新は fetch 時 (主パイプラインの mark_as_read と同設計、
        # crash 時は再試行されないトレードオフを許容)
        seen.update(new_urls)
        self._save_seen(seen)

        return [self._url_to_article(u) for u in new_urls]

    async def init_seen(self) -> int:
        """現状の全 URL を seen として記録する (初回 false-positive 防止)。"""
        all_urls = await self._collect_all_urls()
        filtered = self._filter_urls(all_urls)
        urls = set(filtered)
        self._save_seen(urls)
        _log.info("sitemap_init", watcher=self.name, marked_seen=len(urls))
        return len(urls)

    def read_state(self) -> WatcherState:
        """状態ファイルを読んで UI 表示用スナップショットを返す。"""
        if not self.state_file.exists():
            return WatcherState(
                name=self.name,
                seen_count=0,
                state_file_exists=False,
                last_modified=None,
                state_path=str(self.state_file),
            )
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            seen_count = len(data.get("seen_urls", []))
        except (json.JSONDecodeError, OSError) as e:
            _log.warning("seen_state_read_failed", watcher=self.name, error=str(e))
            seen_count = 0
        mtime = datetime.fromtimestamp(self.state_file.stat().st_mtime).astimezone()
        return WatcherState(
            name=self.name,
            seen_count=seen_count,
            state_file_exists=True,
            last_modified=mtime,
            state_path=str(self.state_file),
        )
