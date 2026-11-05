"""敵対的 host への取得ポリシーの実行部 (2026-10-30)。

分類 (``hostile_hosts``) の結果を、**すべての接続 (feed URL・記事 URL・リダイレクト先)** に
host 単位で適用する。方針は ``docs/fetch_escalation_policy.md`` §6。

- ``HOSTILE_FETCH_POLICY``: ``0`` = 何もしない / ``shadow`` (既定) = 分類してログ
  ``hostile_host_policy`` を出すだけで挙動は不変 / ``1`` = 適用
- ``HOSTILE_FETCH_PAUSE=1``: 手動の停止スイッチ。組織のネットワークに繋がっているときなど、
  hostile 判定の host を**モードに関わらず**一切取りに行かない
- 適用時の挙動: ブラウザ相当 UA のみ (ボット識別は出さない) / Playwright を使わない /
  添付ファイル (PDF・Office・圧縮) を取らない / host ごとに最小間隔 + jitter
  (毎時ちょうどを避ける) / ``Retry-After`` を守る / リダイレクトは 1 ホップごとに再判定

純粋な計算 (``next_slot`` 等) と副作用 (sleep・時計・乱数) は分け、副作用は注入できる。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import random
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from src.logging_config import get_logger
from src.tools.hostile_hosts import (
    PROFILE_HOSTILE,
    REASON_CLASSIFIER_ERROR,
    HostVerdict,
    classify_host,
    normalize_host,
)

_log = get_logger(__name__)

ENV_POLICY = "HOSTILE_FETCH_POLICY"
ENV_PAUSE = "HOSTILE_FETCH_PAUSE"
ENV_MIN_INTERVAL = "HOSTILE_MIN_INTERVAL_SECONDS"
ENV_MAX_WAIT = "HOSTILE_MAX_WAIT_SECONDS"
ENV_INLINE_WAIT = "HOSTILE_INLINE_WAIT_SECONDS"
ENV_GEOIP_DB = "HOSTILE_GEOIP_DB"

PolicyMode = Literal["off", "shadow", "on"]

DEFAULT_MIN_INTERVAL_SECONDS = 120.0
DEFAULT_MAX_WAIT_SECONDS = 300.0
# 記事本文の取得は fetch スロットを握ったまま待たないよう、この秒数を超える待ちは見送る
# (見送った記事は再取得 backlog が次の周期で拾う)。
DEFAULT_INLINE_WAIT_SECONDS = 15.0
JITTER_FRACTION = 0.5  # 最小間隔の 0〜50% を上乗せ
HOUR_AVOID_WINDOW_SECONDS = 45.0  # 毎時 00 分の前後この秒数は避ける
MAX_RETRY_AFTER_SECONDS = 3600.0
MAX_REDIRECT_HOPS = 10
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# 添付ファイルとして取らない拡張子 (URL の path 末尾)。HTML の本文抽出だけを許す。
# .gz は sitemap.xml.gz を巻き込むため含めない。
ATTACHMENT_SUFFIXES: frozenset[str] = frozenset(
    {
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".rtf", ".odt", ".ods",
        ".zip", ".rar", ".7z", ".tar", ".tgz", ".exe", ".msi", ".dmg", ".iso", ".apk",
    }
)  # fmt: skip
_ATTACHMENT_CT_PREFIXES: tuple[str, ...] = (
    "application/pdf",
    "application/msword",
    "application/vnd.ms-",
    "application/vnd.openxmlformats",
    "application/vnd.oasis.opendocument",
    "application/rtf",
    "application/zip",
    "application/x-zip",
    "application/x-rar",
    "application/x-7z",
    "application/x-tar",
    "application/gzip",
    "application/octet-stream",
    "application/x-msdownload",
    "application/x-apple-diskimage",
)


class HostileFetchSkipped(httpx.RequestError):
    """ポリシーにより取得しなかった (ネットワーク失敗ではない)。``reason`` に理由。"""

    def __init__(self, reason: str, url: str) -> None:
        super().__init__(f"hostile_fetch_skipped:{reason}")
        self.reason = reason
        self.url = url


# ---- 設定の解決 (呼出時に env を読む) ----


def policy_mode() -> PolicyMode:
    """``HOSTILE_FETCH_POLICY`` (既定 shadow)。解釈できない値は shadow に倒す。"""
    raw = (os.environ.get(ENV_POLICY) or "shadow").strip().lower()
    if raw in ("0", "off", "false"):
        return "off"
    if raw in ("1", "on", "true"):
        return "on"
    return "shadow"


def is_paused() -> bool:
    return (os.environ.get(ENV_PAUSE) or "").strip() == "1"


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        return default
    return value if value > 0 else default


def min_interval_seconds() -> float:
    return _env_float(ENV_MIN_INTERVAL, DEFAULT_MIN_INTERVAL_SECONDS)


def inline_wait_seconds() -> float:
    return _env_float(ENV_INLINE_WAIT, DEFAULT_INLINE_WAIT_SECONDS)


def max_wait_seconds() -> float:
    return _env_float(ENV_MAX_WAIT, DEFAULT_MAX_WAIT_SECONDS)


# ---- ソース設定の印 (プロセス内) ----

TAG_TTL_SECONDS = 24 * 3600.0
_tagged_hosts: dict[str, float] = {}  # host -> 失効時刻 (monotonic)
_tagged_lock = threading.Lock()
_tag_clock: Callable[[], float] = time.monotonic

# 2 階層目がこれらで、末尾が 2 文字 (国コード) のとき登録ドメインは 3 ラベル (co.uk / com.cn 等)。
_SECOND_LEVEL_LABELS = frozenset({"co", "com", "org", "net", "gov", "ac", "edu", "or", "ne", "go"})


def registered_domain(host: str) -> str:
    """public suffix list を使わない近似の登録ドメイン (example.co.uk → example.co.uk)。"""
    labels = host.split(".")
    if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL_LABELS and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def register_tagged_url(url: str, *, anchor_url: str | None = None) -> bool:
    """``fetch_profile: hostile`` のソースが触れる host を覚える (記事 URL 側の判定に使う)。

    記事 URL の本文取得 (ContentExtractor) はソース設定を持たないため、feed / watcher が
    取得時に host を登録し、以後の記事 URL がこの印を引き継ぐ。
    - ``anchor_url`` (そのソースの feed URL) を渡すと、登録ドメインが同じ host だけ登録する
      (記事が貼る github.com / x.com 等の汎用 host を巻き込んで process 全体を hostile にしない)。
    - 印には TTL (24 時間) があり、feed が再取得されるたびに更新される。
    プロセス内のみ (別プロセスの再取得ジョブは host / ccTLD / ドメインの判定に依る)。
    登録したら True。
    """
    host = normalize_host(url)
    if not host:
        return False
    if anchor_url is not None:
        anchor = normalize_host(anchor_url)
        if not anchor or registered_domain(host) != registered_domain(anchor):
            return False
    with _tagged_lock:
        _tagged_hosts[host] = _tag_clock() + TAG_TTL_SECONDS
    return True


def clear_tagged_hosts() -> None:
    with _tagged_lock:
        _tagged_hosts.clear()


def _is_tagged(host: str | None) -> bool:
    if not host:
        return False
    now = _tag_clock()
    with _tagged_lock:
        for t in [t for t, exp in _tagged_hosts.items() if exp <= now]:
            del _tagged_hosts[t]
        return any(host == t or host.endswith("." + t) for t in _tagged_hosts)


# ---- GeoIP (任意。DB の設定があるときだけ。依存は足さない) ----

_geoip_reader: Any = None
_geoip_failed = False


_geoip_cache: dict[str, tuple[float, frozenset[str]]] = {}
GEOIP_CACHE_TTL_SECONDS = 3600.0


def geoip_configured() -> bool:
    return bool((os.environ.get(ENV_GEOIP_DB) or "").strip()) and not _geoip_failed


def resolve_ip_countries(host: str) -> frozenset[str]:
    """host の解決先 IP の国。``HOSTILE_GEOIP_DB`` 未設定・読込不可なら空 (判定に使わない)。

    ``maxminddb`` が入っている環境でだけ働く (依存を追加しない)。ローカルで名前を引くため、
    プロキシ越しに秘匿したい運用では使わない。
    """
    global _geoip_reader, _geoip_failed  # noqa: PLW0603
    db_path = (os.environ.get(ENV_GEOIP_DB) or "").strip()
    if not db_path or _geoip_failed:
        return frozenset()
    cached = _geoip_cache.get(host)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    try:
        if _geoip_reader is None:
            import maxminddb  # type: ignore[import-not-found,unused-ignore]

            _geoip_reader = maxminddb.open_database(db_path)
        import socket

        countries: set[str] = set()
        for info in socket.getaddrinfo(host, None):
            rec = _geoip_reader.get(info[4][0])
            iso = ((rec or {}).get("country") or {}).get("iso_code")
            if iso:
                countries.add(str(iso).upper())
        result = frozenset(countries)
        _geoip_cache[host] = (time.monotonic() + GEOIP_CACHE_TTL_SECONDS, result)
        return result
    except Exception as e:  # noqa: BLE001 — GeoIP は補助。失敗は無効化して続行
        _geoip_failed = True
        _log.warning("hostile_geoip_unavailable", error=type(e).__name__)
        return frozenset()


# ---- 計画 ----


@dataclass(frozen=True)
class HostilePlan:
    """1 接続 (1 ホップ) に対する判定。"""

    url: str
    verdict: HostVerdict
    mode: PolicyMode
    paused: bool

    @property
    def hostile(self) -> bool:
        return self.verdict.hostile

    @property
    def enforce(self) -> bool:
        """ブラウザ UA のみ・Playwright 禁止・添付拒否・間隔を実際に適用するか。"""
        return self.mode == "on" and self.hostile


def plan_for(url: str, *, source_profile: str | None = None) -> HostilePlan:
    """分類の結果を返す。**分類器が例外を出しても取得を止めない**: その host は hostile 扱いにして
    (fail toward hostile) 理由 ``classifier_error`` を残す。shadow では挙動が変わらない。"""
    try:
        return _plan_for_unsafe(url, source_profile=source_profile)
    except Exception as e:  # noqa: BLE001 — 分類の失敗で取得を落とさない
        _log.warning("hostile_classifier_error", error=type(e).__name__)
        return HostilePlan(
            url=url,
            verdict=HostVerdict(
                host=(url or "")[:80], hostile=True, reasons=(REASON_CLASSIFIER_ERROR,)
            ),
            mode=policy_mode(),
            paused=is_paused(),
        )


def _plan_for_unsafe(url: str, *, source_profile: str | None = None) -> HostilePlan:
    host = normalize_host(url)
    tagged = _is_tagged(host) and (source_profile or "").lower() != PROFILE_HOSTILE
    profile = PROFILE_HOSTILE if tagged else source_profile
    ip_countries = resolve_ip_countries(host) if host else None
    verdict = classify_host(url, source_profile=profile, ip_countries=ip_countries)
    return HostilePlan(url=url, verdict=verdict, mode=policy_mode(), paused=is_paused())


async def aplan_for(url: str, *, source_profile: str | None = None) -> HostilePlan:
    """``plan_for`` の async 版。GeoIP が有効なときだけ DNS 解決を別スレッドに逃がす
    (``getaddrinfo`` でイベントループを止めない)。"""
    if geoip_configured():
        return await asyncio.to_thread(plan_for, url, source_profile=source_profile)
    return plan_for(url, source_profile=source_profile)


def log_plan(plan: HostilePlan, action: str, **extra: object) -> None:
    """``hostile_host_policy`` を記録する (shadow の計測と適用時の追跡の両方)。"""
    if plan.mode == "off" and not plan.paused:
        return
    if not plan.hostile and not extra:
        return
    with contextlib.suppress(Exception):  # 記録の失敗で取得を落とさない
        _log.info(
            "hostile_host_policy",
            host=plan.verdict.host,
            hostile=plan.hostile,
            reasons=list(plan.verdict.reasons),
            mode=plan.mode,
            paused=plan.paused,
            action=action,
            **extra,
        )


def should_skip_playwright(url: str, *, source_profile: str | None = None) -> bool:
    """Playwright (JS 実行) を止めるべきか。hostile + 適用中 / 停止中のとき True。"""
    plan = plan_for(url, source_profile=source_profile)
    if plan.hostile and (plan.enforce or plan.paused):
        log_plan(plan, "skip_playwright")
        return True
    if plan.hostile:
        log_plan(plan, "would_skip_playwright")
    return False


# ---- 添付ファイル ----


def is_attachment_url(url: str) -> bool:
    try:
        path = urlsplit(url).path
    except ValueError:
        return False
    return PurePosixPath(path.lower()).suffix in ATTACHMENT_SUFFIXES


def is_attachment_response(headers: Mapping[str, str]) -> bool:
    """Content-Type / Content-Disposition が添付ファイルを示すか。"""
    disposition = (headers.get("content-disposition") or "").lower()
    if disposition.startswith("attachment"):
        return True
    ctype = (headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    return bool(ctype) and ctype.startswith(_ATTACHMENT_CT_PREFIXES)


# ---- 間隔 (純粋な計算 + 注入できる副作用) ----


def avoid_hour_boundary(ts: float, window: float = HOUR_AVOID_WINDOW_SECONDS) -> float:
    """毎時 00 分の前後 ``window`` 秒に入る時刻を、境界の外へずらす。"""
    offset = ts % 3600.0
    if offset < window:
        return ts - offset + window
    if offset > 3600.0 - window:
        return ts + (3600.0 - offset) + window
    return ts


def next_slot(
    *,
    now: float,
    last_slot: float | None,
    not_before: float,
    interval: float,
    jitter: float,
) -> float:
    """次に取得してよい時刻。最小間隔 + jitter・Retry-After を満たし、毎時の境界を避ける。"""
    earliest = now
    if last_slot is not None:
        earliest = max(earliest, last_slot + interval + jitter)
    return avoid_hour_boundary(max(earliest, not_before))


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """``Retry-After`` (秒 or HTTP-date) を秒にする。不正は None。"""
    if not value or not value.strip():
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - (now or datetime.now(UTC))).total_seconds()
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)


class HostThrottle:
    """host ごとの最小間隔の予約台帳。時計・乱数は注入できる (テストで sleep しない)。"""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        rng: random.Random | None = None,
    ) -> None:
        self._clock = clock
        self._rng = rng or random.Random()  # noqa: S311 — 暗号用途ではない
        self._last: dict[str, float] = {}
        self._not_before: dict[str, float] = {}
        self._lock = threading.Lock()

    def reserve(self, host: str, *, max_wait: float | None = None) -> float | None:
        """host の次の枠を予約し、今から待つ秒数を返す。待ちが上限超なら None (予約しない)。"""
        interval = min_interval_seconds()
        limit = max_wait if max_wait is not None else max_wait_seconds()
        with self._lock:
            now = self._clock()
            slot = next_slot(
                now=now,
                last_slot=self._last.get(host),
                not_before=self._not_before.get(host, 0.0),
                interval=interval,
                jitter=self._rng.uniform(0.0, interval * JITTER_FRACTION),
            )
            if slot - now > limit:
                return None
            self._last[host] = slot
            return max(0.0, slot - now)

    def note_retry_after(self, host: str, seconds: float) -> None:
        with self._lock:
            until = self._clock() + seconds
            self._not_before[host] = max(self._not_before.get(host, 0.0), until)

    def reset(self) -> None:
        with self._lock:
            self._last.clear()
            self._not_before.clear()


_THROTTLE = HostThrottle()


def default_throttle() -> HostThrottle:
    return _THROTTLE


# ---- 1 ホップの前後処理 (async / sync 共通) ----


def prepare_hop(
    url: str,
    *,
    source_profile: str | None,
    throttle: HostThrottle,
    max_wait: float | None = None,
) -> tuple[HostilePlan, float]:
    """接続前の判定。取得しないなら ``HostileFetchSkipped``、する場合は待つ秒数を返す。"""
    plan = plan_for(url, source_profile=source_profile)
    if plan.hostile and plan.paused:
        log_plan(plan, "skip", reason="paused")
        raise HostileFetchSkipped("paused", url)
    if not plan.enforce:
        log_plan(plan, "observe")
        return plan, 0.0
    if is_attachment_url(url):
        log_plan(plan, "skip", reason="attachment_url")
        raise HostileFetchSkipped("attachment_url", url)
    delay = throttle.reserve(plan.verdict.host, max_wait=max_wait)
    if delay is None:
        log_plan(plan, "skip", reason="throttle_deferred")
        raise HostileFetchSkipped("throttle_deferred", url)
    log_plan(plan, "apply", wait_seconds=round(delay, 1))
    return plan, delay


async def _aprepare_hop(
    url: str,
    *,
    source_profile: str | None,
    throttle: HostThrottle,
    max_wait: float | None,
) -> tuple[HostilePlan, float]:
    """``prepare_hop`` の async 版 (GeoIP 有効時だけスレッドへ逃がす)。"""
    if geoip_configured():
        return await asyncio.to_thread(
            prepare_hop, url, source_profile=source_profile, throttle=throttle, max_wait=max_wait
        )
    return prepare_hop(url, source_profile=source_profile, throttle=throttle, max_wait=max_wait)


def check_response(plan: HostilePlan, resp: httpx.Response, throttle: HostThrottle) -> None:
    """応答後の判定 (hostile + 適用中のみ)。Retry-After の記録と添付ファイルの拒否。"""
    if not plan.enforce:
        return
    if resp.status_code in (429, 503):
        wait = parse_retry_after(resp.headers.get("retry-after"))
        if wait:
            throttle.note_retry_after(plan.verdict.host, wait)
    if is_attachment_response(resp.headers):
        log_plan(plan, "skip", reason="attachment_content_type")
        raise HostileFetchSkipped("attachment_content_type", plan.url)


def redirect_target(resp: httpx.Response) -> str | None:
    if resp.status_code not in REDIRECT_STATUSES:
        return None
    location = resp.headers.get("location")
    if not location:
        return None
    return str(resp.url.join(location))


HeadersForHop = Callable[[HostilePlan], dict[str, str] | None]


async def enforced_get_async(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers_for_hop: HeadersForHop,
    source_profile: str | None = None,
    throttle: HostThrottle | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    max_wait: float | None = None,
) -> tuple[httpx.Response, HostilePlan]:
    """リダイレクトを 1 ホップずつ自前で追い、各ホップで判定してから接続する。"""
    gate = throttle or _THROTTLE
    current = url
    for _ in range(MAX_REDIRECT_HOPS + 1):
        plan, delay = await _aprepare_hop(
            current, source_profile=source_profile, throttle=gate, max_wait=max_wait
        )
        if delay > 0:
            await (sleep or asyncio.sleep)(delay)
        request = client.build_request("GET", current, headers=headers_for_hop(plan))
        resp = await client.send(request, stream=True, follow_redirects=False)
        try:
            check_response(plan, resp, gate)
            await resp.aread()
        finally:
            await resp.aclose()
        target = redirect_target(resp)
        if target is None:
            return resp, plan
        current = target
    raise httpx.TooManyRedirects("too many redirects", request=request)


def enforced_get_sync(
    client: httpx.Client,
    url: str,
    *,
    headers_for_hop: HeadersForHop,
    source_profile: str | None = None,
    throttle: HostThrottle | None = None,
    sleep: Callable[[float], object] | None = None,
    max_wait: float | None = None,
) -> tuple[httpx.Response, HostilePlan]:
    """``enforced_get_async`` の sync 版 (ソース追加プレビュー経路)。"""
    gate = throttle or _THROTTLE
    current = url
    for _ in range(MAX_REDIRECT_HOPS + 1):
        plan, delay = prepare_hop(
            current, source_profile=source_profile, throttle=gate, max_wait=max_wait
        )
        if delay > 0:
            (sleep or time.sleep)(delay)
        request = client.build_request("GET", current, headers=headers_for_hop(plan))
        resp = client.send(request, stream=True, follow_redirects=False)
        try:
            check_response(plan, resp, gate)
            resp.read()
        finally:
            resp.close()
        target = redirect_target(resp)
        if target is None:
            return resp, plan
        current = target
    raise httpx.TooManyRedirects("too many redirects", request=request)


def observe_redirect_history(resp: httpx.Response, *, source_profile: str | None = None) -> None:
    """shadow / off の経路: 自動追従された後で、リダイレクト先の host を分類してログだけ残す。"""
    try:
        if policy_mode() == "off":
            return
        for hop in (*resp.history, resp)[1:]:  # 先頭 (要求 URL) は事前に記録済み
            plan = plan_for(str(hop.url), source_profile=source_profile)
            log_plan(plan, "observe_redirect_hop")
    except Exception:  # noqa: BLE001, S110 — 観測の失敗で取得を落とさない
        pass
