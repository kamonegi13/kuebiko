"""FallbackLLMClient (外部→ローカル自動フォールバック、2026-07-19) の unit test。"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from src.tools.llm_client import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    MAX_STRUCTURED_ATTEMPTS,
    LLMClient,
    LLMConnectionError,
    LLMForbiddenModelError,
    LLMResponse,
)
from src.tools.llm_fallback import FallbackLLMClient


class _Out(BaseModel):
    label: str


class _FakeLLM(LLMClient):
    """呼出回数を数え、指定 error を投げられる fake。"""

    def __init__(self, name: str, *, error: Exception | None = None) -> None:
        self._name = name
        self._error = error
        self.calls = 0
        self.last_think: bool | None = None

    @property
    def model(self) -> str:
        return self._name

    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        think: bool | None = None,
    ) -> LLMResponse:
        self.calls += 1
        self.last_think = think
        if self._error is not None:
            raise self._error
        return LLMResponse(text=f"from {self._name}", model=self._name)

    async def generate_structured(
        self,
        prompt: str,
        schema: type[Any],
        system: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        think: bool | None = None,
        max_attempts: int = MAX_STRUCTURED_ATTEMPTS,
    ) -> Any:
        self.calls += 1
        self.last_think = think
        if self._error is not None:
            raise self._error
        return schema(label=self._name)


@pytest.fixture(autouse=True)
def _reset_cooldowns() -> None:
    FallbackLLMClient.reset_cooldowns()


class TestFallback:
    @pytest.mark.asyncio
    async def test_primary_success_no_fallback(self) -> None:
        primary, local = _FakeLLM("ext"), _FakeLLM("local")
        c = FallbackLLMClient(primary=primary, fallback=local)
        resp = await c.generate("p")
        assert resp.text == "from ext"
        assert local.calls == 0
        assert c.model == "ext"  # fallback 未発動なら primary 表記のまま

    @pytest.mark.asyncio
    async def test_availability_error_falls_back(self) -> None:
        primary = _FakeLLM("ext", error=LLMConnectionError("bridge down"))
        local = _FakeLLM("local")
        c = FallbackLLMClient(primary=primary, fallback=local)
        resp = await c.generate("p")
        assert resp.text == "from local"
        # 記録の正直さ: fallback 発動後は両モデル表記
        assert c.model == "ext→local"

    @pytest.mark.asyncio
    async def test_cooldown_skips_primary(self) -> None:
        primary = _FakeLLM("ext", error=LLMConnectionError("rate limited"))
        local = _FakeLLM("local")
        c = FallbackLLMClient(primary=primary, fallback=local)
        await c.generate("p1")
        await c.generate("p2")
        # 2 回目は cooldown 中 → primary を試さない (失敗待ちの無駄なし)
        assert primary.calls == 1
        assert local.calls == 2

    @pytest.mark.asyncio
    async def test_cooldown_shared_across_instances(self) -> None:
        # 同じ primary モデルの別 instance (UI の request 単位構築) にも cooldown が効く
        p1 = _FakeLLM("ext", error=LLMConnectionError("down"))
        c1 = FallbackLLMClient(primary=p1, fallback=_FakeLLM("local"))
        await c1.generate("p")
        p2 = _FakeLLM("ext")
        c2 = FallbackLLMClient(primary=p2, fallback=_FakeLLM("local"))
        await c2.generate("p")
        assert p2.calls == 0

    @pytest.mark.asyncio
    async def test_forbidden_error_never_swallowed(self) -> None:
        primary = _FakeLLM("ext", error=LLMForbiddenModelError("禁止系"))
        local = _FakeLLM("local")
        c = FallbackLLMClient(primary=primary, fallback=local)
        with pytest.raises(LLMForbiddenModelError):
            await c.generate("p")
        assert local.calls == 0

    @pytest.mark.asyncio
    async def test_structured_falls_back(self) -> None:
        primary = _FakeLLM("ext", error=LLMConnectionError("down"))
        local = _FakeLLM("local")
        c = FallbackLLMClient(primary=primary, fallback=local)
        out = await c.generate_structured("p", schema=_Out)
        assert out.label == "local"

    @pytest.mark.asyncio
    async def test_think_on_client_forces_think(self) -> None:
        """ThinkOnClient は呼出元 think に関わらず内側へ True を渡す (factory 方針の注入点)。"""
        from src.tools.llm_fallback import ThinkOnClient

        inner = _FakeLLM("claudecode:sonnet")
        c = ThinkOnClient(inner)
        await c.generate("p", think=False)
        assert inner.last_think is True
        await c.generate_structured("p", schema=_Out, think=False)
        assert inner.last_think is True

        # Fallback と合成しても、外部失敗時のローカルアームは False にクランプされる
        primary = _FakeLLM("claudecode:sonnet", error=LLMConnectionError("down"))
        local = _FakeLLM("gemma4:31b")
        wrapped = ThinkOnClient(FallbackLLMClient(primary=primary, fallback=local))
        await wrapped.generate("p", think=False)
        assert primary.last_think is True
        assert local.last_think is False

    @pytest.mark.asyncio
    async def test_fallback_arm_clamps_think_off(self) -> None:
        """外部 narrative の think=True はローカルアームへ漏らさない (gemma thinking 前歴)。"""
        primary = _FakeLLM("claudecode:sonnet", error=LLMConnectionError("down"))
        local = _FakeLLM("gemma4:31b")
        c = FallbackLLMClient(primary=primary, fallback=local)

        await c.generate("p", think=True)
        assert primary.last_think is True  # 外部には呼出元指定のまま渡す
        assert local.last_think is False  # ローカルは常時クランプ

        FallbackLLMClient.reset_cooldowns()
        await c.generate_structured("p", schema=_Out, think=True)
        assert local.last_think is False
