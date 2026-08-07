"""ClaudeCodeClient (サブスク bridge 経由、2026-07-19) + bridge 純関数の unit test。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from src.tools.claude_code_client import ClaudeCodeClient
from src.tools.llm_client import (
    LLMConnectionError,
    LLMError,
    LLMForbiddenModelError,
    LLMStructuredOutputError,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from claude_code_bridge import (  # noqa: E402
    parse_cli_output,
    summarize_usage,
    validate_bridge_model,
)


class _Out(BaseModel):
    label: str
    score: int


def _bridge_ok(text: str) -> dict[str, Any]:
    return {"text": text, "input_tokens": 10, "output_tokens": 5, "duration_seconds": 1.2}


def _client(handler: Any) -> ClaudeCodeClient:
    return ClaudeCodeClient(
        model="haiku",
        bridge_url="http://bridge.test",
        transport=httpx.MockTransport(handler),
    )


class TestClient:
    def test_forbidden_model_rejected(self) -> None:
        with pytest.raises(LLMForbiddenModelError):
            ClaudeCodeClient(model="qwen-max")

    def test_model_property_has_prefix(self) -> None:
        c = _client(lambda req: httpx.Response(200, json=_bridge_ok("x")))
        assert c.model == "claudecode:haiku"

    @pytest.mark.asyncio
    async def test_usage_recorder_receives_cache_and_cost(self) -> None:
        # 消費台帳の一本化 (2026-07-26): bridge 応答の in/out/duration/cache/cost が
        # recorder (llm_usage 書込) にそのまま渡ること
        recorded: list[tuple[int, int, int, int, float]] = []

        def _rec(
            input_tokens: int,
            output_tokens: int,
            duration_ms: int,
            cache_read_tokens: int = 0,
            cost_usd: float = 0.0,
        ) -> None:
            recorded.append((input_tokens, output_tokens, duration_ms, cache_read_tokens, cost_usd))

        payload = {
            **_bridge_ok("answer"),
            "cache_read_tokens": 2400,
            "cost_usd": 0.034,
        }
        c = ClaudeCodeClient(
            model="haiku",
            bridge_url="http://bridge.test",
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload)),
            usage_recorder=_rec,
        )
        await c.generate("hi")
        assert recorded == [(10, 5, 1200, 2400, 0.034)]

    @pytest.mark.asyncio
    async def test_usage_recorder_failure_does_not_break_call(self) -> None:
        def _boom(*a: object, **k: object) -> None:
            raise RuntimeError("db down")

        c = ClaudeCodeClient(
            model="haiku",
            bridge_url="http://bridge.test",
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json=_bridge_ok("ok"))),
            usage_recorder=_boom,
        )
        resp = await c.generate("hi")
        assert resp.text == "ok"

    @pytest.mark.asyncio
    async def test_generate_parses_bridge_response(self) -> None:
        seen: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(req.content)
            return httpx.Response(200, json=_bridge_ok("こんにちは"))

        resp = await _client(handler).generate("p", system="s")
        assert resp.text == "こんにちは"
        assert resp.input_tokens == 10
        assert seen["body"]["model"] == "haiku"
        assert seen["body"]["system"] == "s"

    @pytest.mark.asyncio
    async def test_connect_error_mentions_bridge(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        with pytest.raises(LLMConnectionError, match="bridge"):
            await _client(handler).generate("p")

    @pytest.mark.asyncio
    async def test_bridge_error_surfaces_detail(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(502, json={"detail": "claude CLI 失敗 (exit 1): auth"})

        with pytest.raises(LLMError, match="claude CLI 失敗"):
            await _client(handler).generate("p")

    @pytest.mark.asyncio
    async def test_structured_parses_json_and_strips_fence(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json=_bridge_ok('```json\n{"label": "apt", "score": 3}\n```')
            )

        out = await _client(handler).generate_structured("p", schema=_Out)
        assert out == _Out(label="apt", score=3)

    @pytest.mark.asyncio
    async def test_structured_invalid_raises_after_attempts(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_bridge_ok("not json at all"))

        with pytest.raises(LLMStructuredOutputError):
            await _client(handler).generate_structured("p", schema=_Out, max_attempts=1)


class TestBridgeHelpers:
    def test_validate_model_aliases(self) -> None:
        assert validate_bridge_model("sonnet") == "sonnet"
        assert validate_bridge_model("claude-haiku-4-5") == "claude-haiku-4-5"

    def test_validate_rejects_injection_and_forbidden(self) -> None:
        with pytest.raises(ValueError):
            validate_bridge_model("sonnet; rm -rf /")
        with pytest.raises(ValueError):
            validate_bridge_model("--dangerously-skip-permissions")
        with pytest.raises(ValueError):
            validate_bridge_model("qwen-max")

    def test_parse_cli_output_success(self) -> None:
        out = parse_cli_output(
            json.dumps(
                {
                    "type": "result",
                    "is_error": False,
                    "result": "答え",
                    "total_cost_usd": 0.12,
                    "usage": {
                        "input_tokens": 7,
                        "output_tokens": 3,
                        "cache_read_input_tokens": 100,
                        "cache_creation_input_tokens": 200,
                    },
                }
            )
        )
        assert out["text"] == "答え"
        assert out["input_tokens"] == 7
        assert out["output_tokens"] == 3
        assert out["cache_read_tokens"] == 100
        assert out["cost_usd"] == 0.12

    def test_parse_cli_output_error_flag(self) -> None:
        with pytest.raises(ValueError, match="エラー"):
            parse_cli_output(json.dumps({"is_error": True, "result": "rate limited"}))

    def test_parse_cli_output_not_json(self) -> None:
        with pytest.raises(ValueError):
            parse_cli_output("plain text")


class TestUsageSummary:
    """bridge のサブスク消費 自己観測 (5h窓/今日/7日)。"""

    def test_windows_aggregate_correctly(self) -> None:
        now = 1_800_000_000.0
        records = [
            {"ts": now - 60, "in": 10, "out": 5, "cache_read": 100, "cost": 0.1},
            {"ts": now - 4 * 3600, "in": 20, "out": 10, "cache_read": 0, "cost": 0.2},
            {"ts": now - 6 * 3600, "in": 40, "out": 20, "cache_read": 0, "cost": 0.4},
            {"ts": now - 8 * 86400, "in": 999, "out": 999, "cache_read": 0, "cost": 9.9},
        ]
        s = summarize_usage(records, now)
        # 5h 窓 = 直近 2 件のみ
        assert s["window_5h"]["calls"] == 2
        assert s["window_5h"]["input_tokens"] == 30
        assert s["window_5h"]["cost_usd_equivalent"] == 0.3
        # 7 日 = 8 日前の 1 件を除く 3 件
        assert s["days7"]["calls"] == 3
        assert s["days7"]["output_tokens"] == 35
        assert s["last_call_at"] is not None

    def test_empty_records(self) -> None:
        s = summarize_usage([], 1_800_000_000.0)
        assert s["window_5h"]["calls"] == 0
        assert s["last_call_at"] is None

    @pytest.mark.asyncio
    async def test_think_false_passed_to_bridge(self) -> None:
        seen: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(req.content)
            return httpx.Response(200, json=_bridge_ok("x"))

        await _client(handler).generate("p", think=False)
        assert seen["body"]["think"] is False

    @pytest.mark.asyncio
    async def test_think_none_omitted(self) -> None:
        seen: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(req.content)
            return httpx.Response(200, json=_bridge_ok("x"))

        await _client(handler).generate("p")
        assert "think" not in seen["body"]
