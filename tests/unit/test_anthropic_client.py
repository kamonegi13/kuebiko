"""AnthropicClient (外部 LLM 開放、2026-07-18) の unit test。

httpx.MockTransport で実ネットワークを切り離し、Messages API の応答整形・
構造化出力 (tool use 強制)・エラー写像・denylist 横断適用を検証する。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from src.tools.anthropic_client import (
    STRUCTURED_TOOL_NAME,
    AnthropicClient,
)
from src.tools.llm_client import (
    LLMConnectionError,
    LLMError,
    LLMForbiddenModelError,
    LLMModelNotFoundError,
    LLMStructuredOutputError,
    LLMTimeoutError,
)


class _Out(BaseModel):
    label: str
    score: int


def _ok_text_response(text: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 12, "output_tokens": 34},
    }


def _ok_tool_response(tool_input: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [
            {"type": "tool_use", "name": STRUCTURED_TOOL_NAME, "input": tool_input},
        ],
        "usage": {"input_tokens": 12, "output_tokens": 34},
    }


def _client(handler: Any, **kwargs: Any) -> AnthropicClient:
    return AnthropicClient(
        model="claude-haiku-4-5",
        api_key="sk-test",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


class TestConstruction:
    def test_missing_api_key_raises(self) -> None:
        with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
            AnthropicClient(model="claude-haiku-4-5", api_key="")

    def test_forbidden_model_rejected_cross_provider(self) -> None:
        # 中華系 denylist はプロバイダ横断 (§4)
        with pytest.raises(LLMForbiddenModelError):
            AnthropicClient(model="qwen-max", api_key="sk-test")

    def test_model_property_includes_provider_prefix(self) -> None:
        c = _client(lambda req: httpx.Response(200, json=_ok_text_response("x")))
        assert c.model == "anthropic:claude-haiku-4-5"


class TestGenerate:
    @pytest.mark.asyncio
    async def test_text_and_usage_extracted(self) -> None:
        seen: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["headers"] = dict(req.headers)
            seen["body"] = json.loads(req.content)
            return httpx.Response(200, json=_ok_text_response("こんにちは"))

        resp = await _client(handler).generate("prompt text", system="sys")
        assert resp.text == "こんにちは"
        assert resp.input_tokens == 12
        assert resp.output_tokens == 34
        assert resp.model == "anthropic:claude-haiku-4-5"
        # API へは素の model 名・system top-level で送られる
        assert seen["body"]["model"] == "claude-haiku-4-5"
        assert seen["body"]["system"] == "sys"
        assert seen["headers"]["x-api-key"] == "sk-test"

    @pytest.mark.asyncio
    async def test_auth_error_maps_to_llm_error(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401, json={"error": {"type": "authentication_error", "message": "bad key"}}
            )

        with pytest.raises(LLMError, match="認証"):
            await _client(handler).generate("p")

    @pytest.mark.asyncio
    async def test_unknown_model_maps_to_not_found(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                404, json={"error": {"type": "not_found_error", "message": "model"}}
            )

        with pytest.raises(LLMModelNotFoundError):
            await _client(handler).generate("p")

    @pytest.mark.asyncio
    async def test_timeout_maps_to_llm_timeout(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow")

        with pytest.raises(LLMTimeoutError):
            await _client(handler).generate("p")

    @pytest.mark.asyncio
    async def test_connect_error_maps_to_llm_connection(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        with pytest.raises(LLMConnectionError):
            await _client(handler).generate("p")


class TestGenerateStructured:
    @pytest.mark.asyncio
    async def test_tool_use_parsed_to_schema(self) -> None:
        seen: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(req.content)
            return httpx.Response(200, json=_ok_tool_response({"label": "apt", "score": 3}))

        out = await _client(handler).generate_structured("p", schema=_Out)
        assert out == _Out(label="apt", score=3)
        # tool use 強制 (tool_choice) で構造化出力を要求している
        assert seen["body"]["tool_choice"] == {"type": "tool", "name": STRUCTURED_TOOL_NAME}

    @pytest.mark.asyncio
    async def test_invalid_then_valid_retries(self) -> None:
        calls = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(200, json=_ok_tool_response({"label": "x"}))  # score 欠落
            return httpx.Response(200, json=_ok_tool_response({"label": "x", "score": 1}))

        out = await _client(handler).generate_structured("p", schema=_Out, max_attempts=2)
        assert out.score == 1
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_no_tool_use_block_raises_after_attempts(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_ok_text_response("not structured"))

        with pytest.raises(LLMStructuredOutputError):
            await _client(handler).generate_structured("p", schema=_Out, max_attempts=1)
