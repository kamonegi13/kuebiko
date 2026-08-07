"""Anthropic (Claude) LLM クライアント — 外部 LLM 開放 (2026-07-18)。

CLAUDE.md §4 改訂により「どの LLM を使うか」は利用者がモデルティア画面で選ぶ。
本 module は ``LLMClient`` 抽象の Anthropic Messages API 実装。**既定はあくまで
ローカル Ollama** (``BUILTIN_MODEL_TIERS``) であり、ティアに ``anthropic:<model>``
を利用者が明示割当した場合のみ使われる (勝手に外部へ送ることはない)。

- 認証: ``.env`` の ``ANTHROPIC_API_KEY`` (AppConfig 経由)。コード直書き禁止。
- 中華系 denylist (§4) はプロバイダ横断で適用 (構築時 ``validate_model_name``)。
- 構造化出力は tool use 強制 (Ollama の format schema と同等以上に頑健)。
- ``think`` 引数は無視する (extended thinking 未使用。要否はプロンプト側で制御
  しており、コスト・レイテンシ増を避ける)。
- API キー・プロンプト本文はログに出さない (§4。token 数と文字数のみ)。
"""

from __future__ import annotations

import time
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from src.logging_config import get_logger
from src.tools.llm_client import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_STRUCTURED_ATTEMPTS,
    LLMClient,
    LLMConnectionError,
    LLMError,
    LLMModelNotFoundError,
    LLMResponse,
    LLMStructuredOutputError,
    LLMTimeoutError,
    UsageRecorder,
    validate_model_name,
)

_log = get_logger(__name__)

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
ANTHROPIC_VERSION = "2023-06-01"


async def list_anthropic_model_ids(api_key: str, *, timeout_seconds: float = 3.0) -> list[str]:
    """Models API から利用可能なモデル id 一覧を取得する (新しい順)。

    モデル選択 UI の動的化用 (ハードコード一覧は将来モデルに追随しないため)。
    失敗時は例外を投げる — 呼出側が curated fallback に degrade する。
    """
    import httpx

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        resp = await client.get(
            f"{ANTHROPIC_MODELS_URL}?limit=100",
            headers={"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION},
        )
        resp.raise_for_status()
        data = resp.json()
    out: list[str] = []
    for item in data.get("data", []):
        mid = str(item.get("id") or "").strip()
        if mid:
            out.append(mid)
    return out


# 構造化出力を強制する tool の名前 (応答の tool_use block を特定するのに使う)。
STRUCTURED_TOOL_NAME = "emit_structured_output"

_T = TypeVar("_T", bound=BaseModel)


class AnthropicClient(LLMClient):
    """Anthropic Messages API への async クライアント (httpx 直、SDK 依存なし)。

    テスト時は ``transport`` に ``httpx.MockTransport`` を渡して実ネットワークを
    切り離せる (OllamaClient の ``client`` 注入と同じ流儀)。
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        base_url: str = ANTHROPIC_API_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        usage_recorder: UsageRecorder | None = None,
    ) -> None:
        validate_model_name(model)
        if not api_key.strip():
            raise LLMError(
                "ANTHROPIC_API_KEY が未設定です (.env に設定するか、"
                "モデルティアをローカル Ollama モデルに戻してください)",
            )
        self._model = model
        self._api_key = api_key.strip()
        self._timeout_seconds = timeout_seconds
        self._base_url = base_url
        self._usage_recorder = usage_recorder
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            transport=transport,
        )

    @property
    def model(self) -> str:
        # ログ / DB 記録では provider を判別できるよう prefix 付きで返す
        return f"anthropic:{self._model}"

    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        think: bool | None = None,  # noqa: ARG002 — 抽象契約互換 (docstring 参照)
    ) -> LLMResponse:
        data, duration = await self._request(self._body(prompt, system, temperature, max_tokens))
        text = "".join(
            str(block.get("text") or "")
            for block in data.get("content") or []
            if isinstance(block, dict) and block.get("type") == "text"
        )
        return self._to_response(data, text=text, duration=duration, structured=False)

    async def generate_structured(
        self,
        prompt: str,
        schema: type[_T],
        system: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        think: bool | None = None,  # noqa: ARG002 — 抽象契約互換 (docstring 参照)
        max_attempts: int = MAX_STRUCTURED_ATTEMPTS,
    ) -> _T:
        """tool use 強制で構造化出力を得る。schema 不正は同条件で最大 max_attempts 回。"""
        body = self._body(prompt, system, temperature, max_tokens)
        body["tools"] = [
            {
                "name": STRUCTURED_TOOL_NAME,
                "description": "指定スキーマの構造化出力を返す",
                "input_schema": schema.model_json_schema(),
            }
        ]
        body["tool_choice"] = {"type": "tool", "name": STRUCTURED_TOOL_NAME}

        last_error: LLMStructuredOutputError | None = None
        attempts = max(1, max_attempts)
        for attempt in range(1, attempts + 1):
            data, duration = await self._request(body)
            tool_input: object = next(
                (
                    block.get("input")
                    for block in data.get("content") or []
                    if isinstance(block, dict) and block.get("type") == "tool_use"
                ),
                None,
            )
            self._to_response(data, text="", duration=duration, structured=True)
            if not isinstance(tool_input, dict):
                last_error = LLMStructuredOutputError(
                    "応答に tool_use block がありません (構造化出力を返さなかった)",
                )
            else:
                try:
                    return schema.model_validate(tool_input)
                except ValidationError as e:
                    last_error = LLMStructuredOutputError(f"スキーマ検証失敗: {e}")
            if attempt < attempts:
                _log.warning(
                    "anthropic_structured_retry",
                    attempt=attempt,
                    model=self._model,
                    reason=str(last_error)[:120],
                )
        assert last_error is not None
        raise last_error

    def _body(
        self,
        prompt: str,
        system: str | None,
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            body["system"] = system
        return body

    async def _request(self, body: dict[str, Any]) -> tuple[dict[str, Any], float]:
        """Messages API を 1 回呼び、(parsed JSON, duration) を返す。エラーは LLM 例外に写像。"""
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        start = time.monotonic()
        try:
            resp = await self._client.post(self._base_url, json=body, headers=headers)
        except httpx.TimeoutException as e:
            raise LLMTimeoutError(
                f"Anthropic 推論タイムアウト ({self._timeout_seconds}s): {e}",
            ) from e
        except httpx.ConnectError as e:
            raise LLMConnectionError(f"Anthropic API に接続できません: {e}") from e
        except httpx.RequestError as e:
            raise LLMConnectionError(f"Anthropic リクエストエラー: {e}") from e
        duration = time.monotonic() - start

        if resp.status_code != 200:
            detail = self._error_detail(resp)
            if resp.status_code == 404:
                raise LLMModelNotFoundError(
                    f"モデル {self._model!r} が Anthropic API に存在しません: {detail}",
                )
            if resp.status_code in (401, 403):
                raise LLMError(f"Anthropic API 認証エラー (API キーを確認): {detail}")
            raise LLMError(f"Anthropic API エラー (HTTP {resp.status_code}): {detail}")

        data = resp.json()
        if not isinstance(data, dict):
            raise LLMError("Anthropic API 応答が JSON object ではありません")
        return data, duration

    @staticmethod
    def _error_detail(resp: httpx.Response) -> str:
        """error payload から安全にメッセージを取り出す (本文全量は載せない)。"""
        try:
            err = resp.json().get("error") or {}
            return str(err.get("message") or "")[:200]
        except (ValueError, AttributeError):
            return resp.text[:200]

    def _to_response(
        self, data: dict[str, Any], *, text: str, duration: float, structured: bool
    ) -> LLMResponse:
        usage = data.get("usage") or {}
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        _log.info(
            "anthropic_response",
            model=self._model,
            duration_seconds=round(duration, 3),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            output_chars=len(text),
            structured=structured,
        )
        if self._usage_recorder is not None:
            try:
                self._usage_recorder(input_tokens, output_tokens, int(duration * 1000))
            except Exception as e:  # noqa: BLE001 — 消費記録で呼出を壊さない
                _log.warning("llm_usage_record_failed", error=str(e)[:120])
        return LLMResponse(
            text=text,
            model=self.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_seconds=duration,
        )
