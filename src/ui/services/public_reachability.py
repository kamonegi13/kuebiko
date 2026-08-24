"""公開面 (Cloudflare Tunnel 経由) の到達性監視。

**なぜ要るか (2026-08-24 の実障害)**: cloudflared がエッジへ QUIC で接続していた際、
**HTTP/2・HTTP/3 のクライアントだけ**が応答途中で切断され (ERR_CONNECTION_CLOSED)、
スマホ / PWA から一切開けない状態が半日続いた。ところが:

- ローカル (127.0.0.1:8001 / :8002) は 2ms で正常
- host から ``curl --http1.1`` でも正常 (HTTP/1.1 は QUIC の影響を受けにくい)
- 死活監視は **ローカルしか見ていなかった**

結果、**利用者の報告が唯一の検知手段**になっていた。ここでは公開 URL を
**HTTP/2 で** 叩き、実際に読み手が通る経路の生死を測る。

⭐ 監視は「利用者と同じ経路・同じプロトコル」で行う。origin が健全であることは
公開面が健全であることを意味しない。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from src.logging_config import get_logger
from src.tools.public_url import resolve_public_base_url

_log = get_logger(__name__)

# 公開面が生きていれば必ず 200 を返す軽量エンドポイント (認証不要・DB を触らない)
_PROBE_PATH = "/api/health"
_TIMEOUT_SECONDS = 15.0
# 一時的な揺らぎで通知を出さないための連続失敗回数
_FAILURES_BEFORE_ALERT = 2
_RETRY_WAIT_SECONDS = 5.0


@dataclass(frozen=True)
class ProbeResult:
    url: str
    ok: bool
    status: int | None
    elapsed_seconds: float
    error: str | None = None


async def _probe_once(url: str) -> ProbeResult:
    import time

    import httpx

    started = time.monotonic()
    try:
        # http2=True: 実障害はこのプロトコルでのみ再現した。HTTP/1.1 で測ると
        # 「正常」に見えてしまい、監視として意味を成さない。
        async with httpx.AsyncClient(http2=True, timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.get(url)
        elapsed = time.monotonic() - started
        return ProbeResult(
            url=url,
            ok=resp.status_code == 200,
            status=resp.status_code,
            elapsed_seconds=round(elapsed, 3),
        )
    except Exception as exc:  # noqa: BLE001 — 監視は落ちない
        return ProbeResult(
            url=url,
            ok=False,
            status=None,
            elapsed_seconds=round(time.monotonic() - started, 3),
            error=f"{type(exc).__name__}: {exc}",
        )


async def check_public_reachability() -> ProbeResult | None:
    """公開 URL を HTTP/2 で叩く。公開していなければ None (監視対象外)。"""
    base = resolve_public_base_url()
    if not base:
        _log.info("public_reachability_skipped", reason="no_public_url")
        return None
    url = base.rstrip("/") + _PROBE_PATH
    result = await _probe_once(url)
    for _ in range(_FAILURES_BEFORE_ALERT - 1):
        if result.ok:
            break
        await asyncio.sleep(_RETRY_WAIT_SECONDS)
        result = await _probe_once(url)
    _log.info(
        "public_reachability_probe",
        ok=result.ok,
        status=result.status,
        elapsed_seconds=result.elapsed_seconds,
        error=result.error,
    )
    return result


async def run_public_reachability_check() -> dict[str, object]:
    """ジョブ入口。到達不能なら ops へ通知する (失敗しても例外は上げない)。"""
    result = await check_public_reachability()
    if result is None:
        return {"skipped": "no_public_url"}
    if not result.ok:
        try:
            from src.ui.services.ops_notify import post_ops_message

            await post_ops_message(
                title="🔴 公開面に到達できません",
                importance="high",
                body="\n".join(
                    [
                        "スマホ / PWA から開けない状態です (ローカルは正常でも起きます)。",
                        f"状態: {result.status or result.error}",
                        "確認: `docker logs tunnel --tail 30` / "
                        "復旧: `docker compose up -d --build tunnel`",
                        "※ HTTP/2 で検査しています。HTTP/1.1 では再現しない故障があります",
                    ]
                ),
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("public_reachability_notify_failed", error=str(exc))
    return {
        "ok": result.ok,
        "status": result.status,
        "elapsed_seconds": result.elapsed_seconds,
        "error": result.error,
    }


__all__ = ["ProbeResult", "check_public_reachability", "run_public_reachability_check"]
