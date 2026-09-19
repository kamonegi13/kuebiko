"""生成が上限に張り付いたことを signal として残す (2026-09-19、SYNTHESIS §54 追記 3)。

凍結評価で「退行の正体は裾 (暴走の率)」と分かったが、**本番で何 % 起きているかを測る術が
無かった**。出力 token が要求上限に達した呼出を記録すれば、モデル横断で率を追える。
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import ollama
import pytest

from src.tools.llm_client import OllamaClient

_MODEL = "gemma4:26b"


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> OllamaClient:
    inner = ollama.AsyncClient(
        host="http://localhost:11434", transport=httpx.MockTransport(handler)
    )
    return OllamaClient(model=_MODEL, client=inner)


def _response(eval_count: int) -> dict[str, object]:
    return {
        "model": _MODEL,
        "created_at": "2026-09-19T00:00:00Z",
        "message": {"role": "assistant", "content": "本文"},
        "done": True,
        "prompt_eval_count": 100,
        "eval_count": eval_count,
    }


@pytest.mark.asyncio
async def test_warns_when_the_generation_fills_the_requested_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.tools.llm_client as mod

    seen: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(mod._log, "warning", lambda ev, **kw: seen.append((ev, kw)))

    client = _client(lambda _r: httpx.Response(200, json=_response(64)))
    await client.generate("問い", max_tokens=64)

    hits = [kw for ev, kw in seen if ev == "llm_output_hit_token_cap"]
    assert hits, "上限到達が記録されていない"
    assert hits[0]["max_tokens"] == 64
    assert hits[0]["model"] == _MODEL


@pytest.mark.asyncio
async def test_silent_when_the_generation_stops_on_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.tools.llm_client as mod

    seen: list[str] = []
    monkeypatch.setattr(mod._log, "warning", lambda ev, **kw: seen.append(ev))

    client = _client(lambda _r: httpx.Response(200, json=_response(63)))
    await client.generate("問い", max_tokens=64)

    assert "llm_output_hit_token_cap" not in seen
