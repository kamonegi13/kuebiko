"""本文取得 User-Agent の自己修復ジョブ (2026-07-27)。

docs/body_extraction_and_entity_integrity_redesign.md §2.2。

ハードコードした UA (Chrome/xxx) はいずれ陳腐化し、WAF がボット署名として 403 を返す
(GBHackers 953/953=100% 切り株の真因)。外部バージョン API に依存せず、**ツール自身の
取得成功を信号に**して自己修復する:

  1. canary = 直近 full 取得できたドメインの代表 URL (repo.sample_healthy_source_urls)。
  2. 現行 UA で canary を probe (raw GET・rotation 無し) して成功率を測る。
  3. 健全 (>= 閾値) なら何もしない。
  4. 劣化時は候補 UA (Chrome major +1..+3 の Mac/Win) を canary で実測検証。
  5. 現行を明確に上回る候補があれば .env + os.environ を更新し #system へ通知。
  6. 全候補も失敗 = UA でなくサイト/ネットワーク側 → 手動レビュー通知 (盲目的に上げない)。

READ_ONLY instance では scheduler 自体が起動しないため、このジョブ (=.env write) は
full instance のみで走る。
"""

from __future__ import annotations

import os
import re

import httpx

from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository
from src.tools.url_guard import UnsafeUrlError, assert_safe_public_url
from src.tools.user_agent import BROWSER_HEADERS, ENV_USER_AGENT, browser_user_agent

_log = get_logger(__name__)

_ENV_KEY = ENV_USER_AGENT
_CHROME_RE = re.compile(r"Chrome/(\d+)")
# 健全とみなす成功率。これ未満なら UA 劣化を疑い候補を試す。
_HEALTHY_RATIO = 0.6
# 採用条件: 候補がこの成功率以上、かつ現行を上回ること。
_ADOPT_RATIO = 0.8
# Chrome major を +1..+_MAX_BUMP まで試す (月 ~1 版 → 週次で 3 ヶ月分吸収)。
_MAX_BUMP = 3
_PROBE_TIMEOUT = 12.0


def _chrome_ua(major: int, *, windows: bool = False) -> str:
    platform = "Windows NT 10.0; Win64; x64" if windows else "Macintosh; Intel Mac OS X 10_15_7"
    return (
        f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )


def _candidate_uas(current_ua: str) -> list[str]:
    """現行 UA の Chrome major から +1..+MAX の候補 (Mac/Win) を組む。"""
    m = _CHROME_RE.search(current_ua)
    if not m:
        return []
    base = int(m.group(1))
    out: list[str] = []
    for bump in range(1, _MAX_BUMP + 1):
        out.append(_chrome_ua(base + bump))
        out.append(_chrome_ua(base + bump, windows=True))
    return out


async def _probe_success_ratio(client: httpx.AsyncClient, ua: str, urls: list[str]) -> float:
    """指定 UA で canary URL 群を GET し、200 の割合を返す (rotation 無しの純 probe)。"""
    if not urls:
        return 0.0
    ok = 0
    for url in urls:
        try:
            assert_safe_public_url(url)
            resp = await client.get(url, headers={"User-Agent": ua, **BROWSER_HEADERS})
        except (httpx.TimeoutException, httpx.RequestError, UnsafeUrlError):
            continue
        if resp.status_code == 200:
            ok += 1
    return ok / len(urls)


async def run_ua_health_check(
    repo: RunHistoryRepository | None = None,
    *,
    canary_limit: int = 8,
) -> dict[str, object]:
    """UA 健全性を canary で検証し、劣化時は検証済みの新 UA に自己修復する。統計 dict を返す。"""
    repo = repo or RunHistoryRepository()
    current = browser_user_agent()
    canaries = repo.sample_healthy_source_urls(limit=canary_limit)
    if not canaries:
        _log.info("ua_health_no_canaries")
        return {"status": "no_canaries", "canaries": 0}

    async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT, follow_redirects=True) as client:
        current_ratio = await _probe_success_ratio(client, current, canaries)
        if current_ratio >= _HEALTHY_RATIO:
            _log.info("ua_health_ok", ratio=round(current_ratio, 2), canaries=len(canaries))
            return {"status": "healthy", "current_ratio": current_ratio, "canaries": len(canaries)}

        # 劣化 → 候補を昇順 (最小 bump 優先) に検証
        best_ua: str | None = None
        best_ratio = current_ratio
        for cand in _candidate_uas(current):
            ratio = await _probe_success_ratio(client, cand, canaries)
            if ratio >= _ADOPT_RATIO and ratio > best_ratio:
                best_ua, best_ratio = cand, ratio
                break  # 最小 bump で閾値を満たしたら採用 (これ以上上げない)

    if best_ua is None:
        # 候補も全滅 = UA でなくサイト/ネットワーク側の問題 → 盲目的に上げず通知のみ
        await _notify(
            "UA 健全性 低下 (手動レビュー推奨)",
            f"現行 UA の canary 成功率 {current_ratio:.0%} だが代替 UA も改善せず。"
            "サイト/ネットワーク側の問題の可能性があります。",
        )
        _log.warning("ua_health_degraded_no_fix", current_ratio=round(current_ratio, 2))
        return {"status": "degraded_no_fix", "current_ratio": current_ratio}

    _adopt_ua(best_ua)
    await _notify(
        "本文取得 UA を自己修復",
        f"canary {len(canaries)} 件で検証し UA を更新しました。"
        f"成功率 {current_ratio:.0%} → {best_ratio:.0%}。新 UA: {best_ua.split(') ', 1)[-1]}",
    )
    _log.info(
        "ua_health_self_healed",
        old_ratio=round(current_ratio, 2),
        new_ratio=round(best_ratio, 2),
        new_ua=best_ua,
    )
    return {
        "status": "self_healed",
        "current_ratio": current_ratio,
        "new_ratio": best_ratio,
        "new_ua": best_ua,
    }


def _adopt_ua(new_ua: str) -> None:
    """新 UA を .env に永続化 + os.environ に即時反映 (構築時解決が次回から拾う)。"""
    from pathlib import Path

    from src.ui.services.env_editor import update_env

    os.environ[_ENV_KEY] = new_ua  # in-process 即時反映 (再起動不要)
    try:
        # .env は CWD 直下 (AppConfig の env_file=".env" と同じ、コンテナは CWD=/app)。
        update_env(Path(".env"), {_ENV_KEY: new_ua})
    except Exception as e:  # noqa: BLE001 — 永続化失敗でも in-process は効く
        _log.warning("ua_env_persist_failed", error=str(e))


async def _notify(title: str, body: str) -> None:
    """ops チャンネルへ通知 (READ_ONLY/suppress は helper が処理、失敗は握り潰す)。"""
    from src.ui.services.ops_notify import post_ops_message

    await post_ops_message(title=title, body=body)
