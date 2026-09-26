"""一時的な Discord 障害を数分おきに試し直す (discord_publisher.post_patiently、2026-09-26)。"""

from __future__ import annotations

from typing import Any

import pytest

from src.tools.discord_publisher import DiscordPostError, post_patiently


class _Publisher:
    def __init__(self, statuses: list[int | None]) -> None:
        self._statuses = list(statuses)  # None = 成功
        self.calls = 0

    async def post(self, message: Any) -> str:
        self.calls += 1
        status = self._statuses.pop(0)
        if status is not None:
            raise DiscordPostError(f"HTTP {status}", status=status)
        return "ok"


async def _no_sleep(_: float) -> None:
    return None


async def test_transient_5xx_is_retried_until_success() -> None:
    pub = _Publisher([500, 502, None])
    assert await post_patiently(pub, "m", waits=(1, 1, 1), sleep=_no_sleep) == "ok"
    assert pub.calls == 3


async def test_gives_up_after_all_waits() -> None:
    pub = _Publisher([503, 503, 503])
    with pytest.raises(DiscordPostError):
        await post_patiently(pub, "m", waits=(1, 1), sleep=_no_sleep)
    assert pub.calls == 3


async def test_client_error_is_not_retried() -> None:
    """webhook の失効 (404) や権限 (403) は待っても直らない。"""
    pub = _Publisher([404])
    with pytest.raises(DiscordPostError):
        await post_patiently(pub, "m", waits=(1, 1), sleep=_no_sleep)
    assert pub.calls == 1


async def test_rate_limit_is_retried() -> None:
    pub = _Publisher([429, None])
    assert await post_patiently(pub, "m", waits=(1,), sleep=_no_sleep) == "ok"
