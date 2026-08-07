"""Claude Code サブスク経由の LLM クライアント (2026-07-19)。

``LLMClient`` 抽象の実装。ホスト側 bridge (scripts/claude_code_bridge.py) に HTTP で
接続し、Claude サブスクリプション (Pro/Max) の枠で推論する — API クレジット不要。
ティア割当が ``claudecode:<sonnet|haiku|opus>`` のとき ``build_llm_for`` が構築する。

制約 (bridge 側 README 兼ねて明記):
- サブスクのレート上限 (5 時間窓) を共有する — 低頻度ティア (reasoning/dialog) 向け。
  fast (収集系 ~2500 call/日) への割当は上限的に成立しない
- 構造化出力は tool 強制が無いため「schema 同梱プロンプト + pydantic 検証 + リトライ」
  (Ollama の format 強制よりは弱いが、Claude 系は JSON 追従が堅牢)
- ``think`` は bridge に透過する。CLI 既定は thinking ON で複雑タスクでは思考が
  6-8k tok/call = 時間の ~8 割を占めるため、呼出元の ``think=False`` (本 repo の
  digest/synthesis/チャット系はすべて明示) を尊重して無効化する (実測 71s→8s)
"""

from __future__ import annotations

import json
import re
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
    LLMResponse,
    LLMStructuredOutputError,
    LLMTimeoutError,
    UsageRecorder,
    _repair_truncated_json,
    validate_model_name,
)

_log = get_logger(__name__)

DEFAULT_BRIDGE_URL = "http://host.docker.internal:8010"

# 出力からコードフェンスを剥がす (JSON のみ指示でも稀に付く)
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)

_T = TypeVar("_T", bound=BaseModel)


class ClaudeCodeClient(LLMClient):
    """ホスト側 claude-code-bridge への async クライアント。

    テスト時は ``transport`` に ``httpx.MockTransport`` を渡して切り離す。
    """

    def __init__(
        self,
        model: str,
        bridge_url: str = DEFAULT_BRIDGE_URL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        usage_recorder: UsageRecorder | None = None,
    ) -> None:
        validate_model_name(model)
        self._model = model
        self._bridge_url = bridge_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        # 消費台帳の一本化 (2026-07-26): llm_usage へ (in, out, duration_ms,
        # cache_read, cost_usd) を記録。bridge 側 JSONL は内部 debug 用として併存。
        self._usage_recorder = usage_recorder
        # bridge 側の CLI 実行分の余裕を持たせる (+10s)
        self._client = httpx.AsyncClient(timeout=timeout_seconds + 10.0, transport=transport)

    @property
    def model(self) -> str:
        return f"claudecode:{self._model}"

    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,  # noqa: ARG002 — CLI 側に制御が無い
        max_tokens: int = DEFAULT_MAX_TOKENS,  # noqa: ARG002 — 同上
        think: bool | None = None,
    ) -> LLMResponse:
        data = await self._request(prompt, system, think)
        return LLMResponse(
            text=str(data.get("text") or ""),
            model=self.model,
            input_tokens=int(data.get("input_tokens") or 0),
            output_tokens=int(data.get("output_tokens") or 0),
            duration_seconds=float(data.get("duration_seconds") or 0.0),
        )

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
        """schema 同梱プロンプトで JSON を要求し、pydantic 検証 + リトライで固める。"""
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
                        _log.warning("claudecode_structured_salvaged", attempt=attempt)
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
                    "claudecode_structured_retry",
                    attempt=attempt,
                    model=self._model,
                    reason=str(last_error)[:120],
                )
        assert last_error is not None
        raise last_error

    async def _request(
        self, prompt: str, system: str | None, think: bool | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "prompt": prompt,
            "model": self._model,
            "timeout_seconds": self._timeout_seconds,
        }
        if system:
            body["system"] = system
        if think is not None:
            body["think"] = think
        try:
            resp = await self._client.post(f"{self._bridge_url}/v1/generate", json=body)
        except httpx.TimeoutException as e:
            raise LLMTimeoutError(
                f"claude-code-bridge タイムアウト ({self._timeout_seconds}s): {e}",
            ) from e
        except httpx.ConnectError as e:
            raise LLMConnectionError(
                f"claude-code-bridge に接続できません ({self._bridge_url}) — "
                f"ホストで scripts/claude_code_bridge.py が起動しているか確認: {e}",
            ) from e
        except httpx.RequestError as e:
            raise LLMConnectionError(f"claude-code-bridge リクエストエラー: {e}") from e

        if resp.status_code == 504:
            raise LLMTimeoutError(f"claude CLI タイムアウト: {self._detail(resp)}")
        if resp.status_code != 200:
            raise LLMError(
                f"claude-code-bridge エラー (HTTP {resp.status_code}): {self._detail(resp)}",
            )
        data = resp.json()
        if not isinstance(data, dict):
            raise LLMError("claude-code-bridge 応答が JSON object ではありません")
        _log.info(
            "claudecode_response",
            model=self._model,
            duration_seconds=data.get("duration_seconds"),
            input_tokens=int(data.get("input_tokens") or 0),
            output_tokens=int(data.get("output_tokens") or 0),
            output_chars=len(str(data.get("text") or "")),
        )
        if self._usage_recorder is not None:
            try:
                self._usage_recorder(
                    int(data.get("input_tokens") or 0),
                    int(data.get("output_tokens") or 0),
                    int(float(data.get("duration_seconds") or 0.0) * 1000),
                    int(data.get("cache_read_tokens") or 0),
                    float(data.get("cost_usd") or 0.0),
                )
            except Exception as e:  # noqa: BLE001 — 消費記録の失敗で LLM 呼出を壊さない
                _log.warning("llm_usage_record_failed", error=str(e)[:120])
        return data

    @staticmethod
    def _detail(resp: httpx.Response) -> str:
        try:
            return str(resp.json().get("detail") or "")[:300]
        except (ValueError, AttributeError):
            return resp.text[:300]
