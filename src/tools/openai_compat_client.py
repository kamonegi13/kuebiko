"""OpenAI Chat Completions 互換クライアント — 接続先レジストリ (2026-07-24)。

OpenAI / Gemini (互換モード) / LM Studio / リモート Ollama / 将来のプロバイダは、
事実上の業界標準である OpenAI Chat Completions API を話す。ベンダー別クライアントを
並べる代わりに、本実装 1 つ (base_url + API キー + モデル名で構成) で対応する。

- モデル参照は ``<接続先名>:<モデル>``。接続先定義は DB (llm_endpoints)、
  キーは .env (``LLM_ENDPOINT_KEY_<NAME>``) — §4「認証情報は .env」に従う。
- 中華系 denylist (§4) はプロバイダ横断 (構築時 ``validate_model_name``)。
- ``think`` は無視 (reasoning 制御はプロバイダ非統一。Anthropic API 直と同じ扱い —
  narrative の ThinkOnClient が True を渡しても無害)。
- 構造化出力: response_format には依存せず schema 同梱プロンプト + salvage
  (claudecode bridge と同方式 — 互換実装間で最も可搬)。
- トークン消費は usage_recorder 経由で llm_usage へ記録 (モデルタブの消費表示用)。
  記録失敗は握る — 消費記録で LLM 呼出を壊さない。
- API キー・プロンプト本文はログに出さない (§4。token 数と文字数のみ)。
"""

from __future__ import annotations

import json
import re
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
    _repair_truncated_json,
    validate_model_name,
)

_log = get_logger(__name__)

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)

_T = TypeVar("_T", bound=BaseModel)

# 消費記録 callback: 共有 Protocol (llm_client.UsageRecorder) を使う。
# 実体は model_tiers factory が llm_usage テーブルへの書込として注入する。


class OpenAICompatClient(LLMClient):
    """OpenAI Chat Completions 互換 API への async クライアント (httpx 直)。

    テスト時は ``transport`` に ``httpx.MockTransport`` を渡す (他 client と同じ流儀)。
    """

    def __init__(
        self,
        endpoint_name: str,
        model: str,
        base_url: str,
        api_key: str = "",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        usage_recorder: UsageRecorder | None = None,
    ) -> None:
        validate_model_name(model)
        self._endpoint_name = endpoint_name
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key.strip()
        self._timeout_seconds = timeout_seconds
        self._usage_recorder = usage_recorder
        self._client = httpx.AsyncClient(timeout=timeout_seconds, transport=transport)

    @property
    def model(self) -> str:
        # ログ / DB 記録では接続先を判別できるよう prefix 付きで返す
        return f"{self._endpoint_name}:{self._model}"

    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        think: bool | None = None,  # noqa: ARG002 — 抽象契約互換 (docstring 参照)
    ) -> LLMResponse:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        data, duration = await self._request(body)
        choices = data.get("choices") or []
        text = ""
        if choices and isinstance(choices[0], dict):
            text = str((choices[0].get("message") or {}).get("content") or "")
        usage = data.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        _log.info(
            "openai_compat_response",
            endpoint=self._endpoint_name,
            model=self._model,
            duration_seconds=round(duration, 3),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            output_chars=len(text),
        )
        if self._usage_recorder is not None:
            try:
                self._usage_recorder(input_tokens, output_tokens, int(duration * 1000))
            except Exception as e:  # noqa: BLE001 — 消費記録で呼出を壊さない
                _log.warning("llm_usage_record_failed", error=str(e)[:120])
        return LLMResponse(text=text, model=self.model)

    async def generate_structured(
        self,
        prompt: str,
        schema: type[_T],
        system: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        think: bool | None = None,
        max_attempts: int = MAX_STRUCTURED_ATTEMPTS,
    ) -> _T:
        """schema 同梱プロンプトで JSON を要求し、pydantic 検証 + salvage + リトライで固める。"""
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        structured_prompt = (
            f"{prompt}\n\n"
            "# 出力形式 (厳守)\n"
            "以下の JSON Schema に厳密に従う JSON オブジェクト**のみ**を出力すること。\n"
            "説明文・前置き・コードフェンスは一切付けない。\n"
            f"{schema_json}"
        )
        last_error: LLMStructuredOutputError | None = None
        attempts = max(1, max_attempts)
        for attempt in range(1, attempts + 1):
            resp = await self.generate(
                structured_prompt,
                system=system,
                temperature=temperature,
                max_tokens=max_tokens,
                think=think,
            )
            text = _FENCE_RE.sub("", resp.text).strip()
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                salvaged = _repair_truncated_json(text)
                if salvaged is not None:
                    try:
                        obj = schema.model_validate(salvaged)
                        _log.warning(
                            "openai_compat_structured_salvaged",
                            endpoint=self._endpoint_name,
                            attempt=attempt,
                        )
                        return obj
                    except ValidationError:
                        pass
                last_error = LLMStructuredOutputError(
                    f"JSON パース失敗: {e}; 出力先頭 200: {text[:200]!r}",
                )
            else:
                try:
                    return schema.model_validate(data)
                except ValidationError as e:
                    last_error = LLMStructuredOutputError(f"スキーマ検証失敗: {e}")
            if attempt < attempts:
                _log.warning(
                    "openai_compat_structured_retry",
                    endpoint=self._endpoint_name,
                    attempt=attempt,
                    reason=str(last_error)[:120],
                )
        assert last_error is not None
        raise last_error

    async def _request(self, body: dict[str, Any]) -> tuple[dict[str, Any], float]:
        """chat/completions を 1 回呼び (parsed JSON, duration) を返す。エラーは LLM 例外へ写像。"""
        headers = {"content-type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        start = time.monotonic()
        try:
            resp = await self._client.post(
                f"{self._base_url}/chat/completions", json=body, headers=headers
            )
        except httpx.TimeoutException as e:
            raise LLMTimeoutError(
                f"{self._endpoint_name} 推論タイムアウト ({self._timeout_seconds}s): {e}",
            ) from e
        except httpx.ConnectError as e:
            raise LLMConnectionError(f"{self._endpoint_name} に接続できません: {e}") from e
        except httpx.RequestError as e:
            raise LLMConnectionError(f"{self._endpoint_name} リクエストエラー: {e}") from e
        duration = time.monotonic() - start
        if resp.status_code == 404:
            raise LLMModelNotFoundError(
                f"{self._endpoint_name}: モデル '{self._model}' が見つかりません"
                f" (HTTP 404): {resp.text[:200]}",
            )
        if resp.status_code != 200:
            raise LLMError(
                f"{self._endpoint_name} API エラー (HTTP {resp.status_code}): {resp.text[:300]}",
            )
        data = resp.json()
        if not isinstance(data, dict):
            raise LLMError(f"{self._endpoint_name} 応答が JSON object ではありません")
        return data, duration


async def list_openai_compat_model_ids(
    base_url: str, api_key: str = "", *, timeout_seconds: float = 3.0
) -> list[str]:
    """互換 API の /models からモデル id 一覧を取得 (LM Studio 等も対応)。

    失敗時は例外を投げる — 呼出側が空リストへ degrade する。
    """
    headers = {}
    if api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        resp = await client.get(f"{base_url.rstrip('/')}/models", headers=headers)
        resp.raise_for_status()
        data = resp.json()
    out: list[str] = []
    for item in data.get("data", []):
        mid = str(item.get("id") or "").strip()
        if mid:
            out.append(mid)
    return out
