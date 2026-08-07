"""src.tools.llm_client のテスト (Step 6)。

httpx.MockTransport で Ollama API をモックする。実 Ollama は使わない。
中国系モデル名のホワイトリスト検証 (CLAUDE.md §4) を必ずテストに含める。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import cast

import httpx
import ollama
import pytest
from pydantic import BaseModel

from src.tools.llm_client import (
    DEFAULT_MODEL,
    LLMConnectionError,
    LLMForbiddenModelError,
    LLMModelNotFoundError,
    LLMResponse,
    LLMStructuredOutputError,
    OllamaClient,
    validate_model_name,
)

# ---------- ヘルパ ----------


def _build_ollama_client(
    handler: Callable[[httpx.Request], httpx.Response],
    model: str = DEFAULT_MODEL,
) -> OllamaClient:
    """httpx.MockTransport を仕込んだ ollama.AsyncClient を持つ OllamaClient。"""
    transport = httpx.MockTransport(handler)
    inner = ollama.AsyncClient(host="http://localhost:11434", transport=transport)
    return OllamaClient(model=model, client=inner)


def _ok_chat_response(
    content: str = "Hello",
    model: str = DEFAULT_MODEL,
    prompt_eval_count: int = 10,
    eval_count: int = 5,
) -> dict[str, object]:
    """Ollama /api/chat の標準レスポンス JSON。"""
    return {
        "model": model,
        "created_at": "2026-05-01T00:00:00Z",
        "message": {"role": "assistant", "content": content},
        "done": True,
        "total_duration": 1234567,
        "prompt_eval_count": prompt_eval_count,
        "eval_count": eval_count,
    }


# ---------- ホワイトリスト検証 ----------


class TestModelNameValidation:
    @pytest.mark.parametrize(
        "model",
        [
            "qwen3:7b",
            "Qwen3-coder",
            "qwen2.5:35b",
            "deepseek-r1:14b",
            "DeepSeek-Coder:6.7b",
            "yi:34b",
            "yi-9b",
            "glm-4",
            "GLM4-9b",
            "chatglm3:6b",
            "internlm2:7b",
            "m3e-base",
            "bge-large-zh",
            "bge_small_en",
            # 回帰: namespace 付き (Ollama の HuggingFace 経路) は bare 名で捕捉する。
            # これらは旧実装 (full 名のみ startswith) では §4 防御をすり抜けていた。
            "hf.co/Qwen/Qwen2.5-7B-Instruct-GGUF",
            "BAAI/bge-large-en",
            "org/deepseek-r1:7b",
            "registry.example.com/library/qwen3:7b",
        ],
    )
    def test_forbidden_model_raises(self, model: str) -> None:
        with pytest.raises(LLMForbiddenModelError):
            validate_model_name(model)

    @pytest.mark.parametrize(
        "model",
        [
            "gemma4:31b",
            "gemma3:27b",
            "llama3.1:8b",
            "mistral:7b",
            "phi3:14b",
            "GEMMA4:31B",
            # namespace 付きの正当モデルは誤検知しないこと (bare 名でも prefix に当たらない)
            "intfloat/multilingual-e5-large-instruct",
            "hf.co/Snowflake/snowflake-arctic-embed2",
        ],
    )
    def test_allowed_model_passes(self, model: str) -> None:
        validate_model_name(model)

    def test_empty_model_raises(self) -> None:
        with pytest.raises(LLMForbiddenModelError):
            validate_model_name("")

    def test_ollama_client_init_rejects_forbidden_model(self) -> None:
        with pytest.raises(LLMForbiddenModelError):
            OllamaClient(model="qwen3:7b")

    def test_ollama_client_init_accepts_gemma(self) -> None:
        # ollama.AsyncClient は実 host に繋がないので作るだけは可能
        client = OllamaClient(model="gemma4:31b")
        assert client.model == "gemma4:31b"


# ---------- LLMResponse ----------


class TestLLMResponse:
    def test_is_frozen(self) -> None:
        r = LLMResponse(text="x", model="gemma4:31b")
        with pytest.raises(Exception):  # noqa: B017, BLE001
            r.text = "mutated"


# ---------- generate (free-form text) ----------


class TestGenerate:
    @pytest.mark.asyncio
    async def test_returns_message_content_and_token_counts(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_ok_chat_response("こんにちは"))

        client = _build_ollama_client(handler)
        resp = await client.generate("Hi")

        assert isinstance(resp, LLMResponse)
        assert resp.text == "こんにちは"
        assert resp.model == DEFAULT_MODEL
        assert resp.input_tokens == 10
        assert resp.output_tokens == 5
        assert resp.duration_seconds >= 0.0

    @pytest.mark.asyncio
    async def test_passes_system_prompt(self) -> None:
        seen_messages: list[list[dict[str, object]]] = []

        def handler(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content.decode())
            seen_messages.append(body.get("messages", []))
            return httpx.Response(200, json=_ok_chat_response())

        client = _build_ollama_client(handler)
        await client.generate("user prompt", system="be concise")

        msgs = seen_messages[0]
        assert msgs[0] == {"role": "system", "content": "be concise"}
        assert msgs[1] == {"role": "user", "content": "user prompt"}

    @pytest.mark.asyncio
    async def test_passes_temperature_and_max_tokens(self) -> None:
        seen_options: list[dict[str, object]] = []

        def handler(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content.decode())
            seen_options.append(body.get("options", {}))
            return httpx.Response(200, json=_ok_chat_response())

        client = _build_ollama_client(handler)
        await client.generate("hi", temperature=0.7, max_tokens=512)

        assert seen_options[0]["temperature"] == 0.7
        assert seen_options[0]["num_predict"] == 512

    @pytest.mark.asyncio
    async def test_no_format_field_in_free_form_request(self) -> None:
        seen_bodies: list[dict[str, object]] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen_bodies.append(json.loads(req.content.decode()))
            return httpx.Response(200, json=_ok_chat_response())

        client = _build_ollama_client(handler)
        await client.generate("hi")

        assert "format" not in seen_bodies[0]


# ---------- generate_structured ----------


class _Summary(BaseModel):
    bluf: str
    importance: str
    sources: list[str]


class TestGenerateStructured:
    @pytest.mark.asyncio
    async def test_parses_valid_json_into_schema(self) -> None:
        valid = {
            "bluf": "新たな APT が発見された",
            "importance": "high",
            "sources": ["https://example.com/a"],
        }

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_ok_chat_response(json.dumps(valid)))

        client = _build_ollama_client(handler)
        result = await client.generate_structured("prompt", schema=_Summary)

        assert isinstance(result, _Summary)
        assert result.bluf == "新たな APT が発見された"
        assert result.sources == ["https://example.com/a"]

    @pytest.mark.asyncio
    async def test_invalid_json_raises_structured_output_error(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_ok_chat_response("not json {{{"))

        client = _build_ollama_client(handler)
        with pytest.raises(LLMStructuredOutputError, match="JSON パース失敗"):
            await client.generate_structured("p", schema=_Summary)

    @pytest.mark.asyncio
    async def test_schema_violation_raises_structured_output_error(self) -> None:
        # importance が必須なのに無い
        invalid = {"bluf": "x"}

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_ok_chat_response(json.dumps(invalid)))

        client = _build_ollama_client(handler)
        with pytest.raises(LLMStructuredOutputError, match="スキーマ検証失敗"):
            await client.generate_structured("p", schema=_Summary)

    @pytest.mark.asyncio
    async def test_format_schema_is_passed_to_ollama(self) -> None:
        seen_formats: list[object] = []

        def handler(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content.decode())
            seen_formats.append(body.get("format"))
            return httpx.Response(
                200,
                json=_ok_chat_response(
                    json.dumps(
                        {
                            "bluf": "x",
                            "importance": "low",
                            "sources": [],
                        },
                    ),
                ),
            )

        client = _build_ollama_client(handler)
        await client.generate_structured("p", schema=_Summary)

        # format に dict 型のスキーマが入っている (str ではない)
        fmt = seen_formats[0]
        assert isinstance(fmt, dict)
        assert "properties" in fmt
        assert "bluf" in fmt["properties"]


# ---------- Phase 5P: structured output 1 回リトライ ----------


class TestGenerateStructuredRetry:
    @pytest.mark.asyncio
    async def test_retries_once_on_json_decode_error(self) -> None:
        """1 回目 JSON 崩れ → 2 回目 valid → 成功 (リトライ 1 回)。"""
        call_count = 0
        valid = json.dumps({"bluf": "x", "importance": "high", "sources": []})

        def handler(req: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            text = "garbage{{{" if call_count == 1 else valid
            return httpx.Response(200, json=_ok_chat_response(text))

        client = _build_ollama_client(handler)
        result = await client.generate_structured("p", schema=_Summary)
        assert result.importance == "high"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_retries_once_on_validation_error(self) -> None:
        """1 回目 schema 不正 → 2 回目 valid → 成功。"""
        call_count = 0
        valid = json.dumps({"bluf": "x", "importance": "low", "sources": []})

        def handler(req: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            text = json.dumps({"bluf": "missing_importance"}) if call_count == 1 else valid
            return httpx.Response(200, json=_ok_chat_response(text))

        client = _build_ollama_client(handler)
        result = await client.generate_structured("p", schema=_Summary)
        assert result.bluf == "x"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_does_not_retry_more_than_once(self) -> None:
        """2 回連続失敗で raise (合計 2 回試行で打ち切り)。"""
        call_count = 0

        def handler(req: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(200, json=_ok_chat_response("not json"))

        client = _build_ollama_client(handler)
        with pytest.raises(LLMStructuredOutputError):
            await client.generate_structured("p", schema=_Summary)
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_logs_llm_structured_retry_event(self) -> None:
        from unittest.mock import patch

        call_count = 0
        valid = json.dumps({"bluf": "x", "importance": "low", "sources": []})

        def handler(req: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            text = "garbage" if call_count == 1 else valid
            return httpx.Response(200, json=_ok_chat_response(text))

        client = _build_ollama_client(handler)
        with patch("src.tools.llm_client._log") as mock_log:
            await client.generate_structured("p", schema=_Summary)

        retry_calls = [
            c
            for c in mock_log.warning.call_args_list
            if c.args and c.args[0] == "llm_structured_retry"
        ]
        assert len(retry_calls) == 1


# ---------- エラー系 ----------


class TestErrorMapping:
    @pytest.mark.asyncio
    async def test_404_raises_model_not_found(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                404,
                json={"error": "model 'gemma4:31b' not found"},
            )

        client = _build_ollama_client(handler)
        with pytest.raises(LLMModelNotFoundError):
            await client.generate("hi")

    @pytest.mark.asyncio
    async def test_connection_error_raises_llm_connection_error(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("simulated refused")

        client = _build_ollama_client(handler)
        with pytest.raises(LLMConnectionError):
            await client.generate("hi")

    @pytest.mark.asyncio
    async def test_other_5xx_raises_generic_llm_error(self) -> None:
        from src.tools.llm_client import LLMError

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": "service unavailable"})

        client = _build_ollama_client(handler)
        with pytest.raises(LLMError):
            await client.generate("hi")


class TestRepairTruncatedJson:
    """Phase B: Ollama structured output の末尾切断 JSON 救済。"""

    def test_repairs_unterminated_string(self) -> None:
        from src.tools.llm_client import _repair_truncated_json

        # summary 文字列の途中で切断された JSON
        truncated = (
            '{\n  "title_ja": "ウクライナ無人機がフィンランドに墜落",\n'
            '  "importance": "high",\n  "category": "geopolitical",\n'
            '  "summary": "ロシアの電子戦が原因か。詳細は調査中で'
        )
        obj = _repair_truncated_json(truncated)
        assert obj is not None
        assert cast(str, obj["title_ja"]).startswith("ウクライナ")
        assert obj["importance"] == "high"
        assert obj["category"] == "geopolitical"
        assert "summary" in obj  # 切断された summary も (短いが) 復元

    def test_repairs_unterminated_array(self) -> None:
        from src.tools.llm_client import _repair_truncated_json

        truncated = '{"a": "x", "iocs": ["1.2.3.4", "evil.com"'
        obj = _repair_truncated_json(truncated)
        assert obj is not None
        assert obj["a"] == "x"
        assert obj["iocs"] == ["1.2.3.4", "evil.com"]

    def test_returns_none_when_no_object(self) -> None:
        from src.tools.llm_client import _repair_truncated_json

        assert _repair_truncated_json("no json here") is None
