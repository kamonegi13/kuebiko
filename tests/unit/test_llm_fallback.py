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
    LLMRefusalError,
    LLMResponse,
    LLMStructuredOutputError,
)
from src.tools.llm_fallback import FAILURE_STREAK, REQUEST_RETRIES, FallbackLLMClient


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


class TestPerRequestFailureDoesNotCooldown:
    """1 件だけの失敗 (拒否・JSON 崩れ) で経路全体を止めない。

    混ぜると 1 本の悪い記事が後続 10 分の生成を巻き添えにし、バッチが「枠切れ」と
    誤認して止まる (2026-08-27 に遡及が 2 度停止した)。
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("structured", [False, True])
    async def test_refusal_falls_back_without_blocking_the_next_call(
        self, structured: bool
    ) -> None:
        # Arrange — 1 回目は拒否、2 回目からは通る primary
        FallbackLLMClient.reset_cooldowns()
        # 再試行 (1 回) でも回復しない = 先頭 2 回が失敗する fake
        primary = _FailOnceLLM("claudecode:sonnet", fails=1 + REQUEST_RETRIES)
        local = _FakeLLM("gemma4:31b")
        client = FallbackLLMClient(primary=primary, fallback=local)

        # Act — 同じ client で 2 回呼ぶ
        if structured:
            await client.generate_structured("p", _Out)
            await client.generate_structured("p", _Out)
        else:
            await client.generate("p")
            await client.generate("p")

        # Assert — 2 回目も primary を試している (cooldown に入っていない)
        # 1 回目: 本試行 + 再試行 = 2 回 → ローカルへ。2 回目: cooldown に入って
        # いないので再び primary を試す
        assert primary.calls == 2 + 1
        assert local.calls == 1

    @pytest.mark.asyncio
    async def test_unreachable_route_still_cools_down_immediately(self) -> None:
        # Arrange — 接続不可は経路の問題なので 1 回で cooldown する
        FallbackLLMClient.reset_cooldowns()
        primary = _FakeLLM("claudecode:sonnet", error=LLMConnectionError("down"))
        local = _FakeLLM("gemma4:31b")
        client = FallbackLLMClient(primary=primary, fallback=local)

        # Act
        await client.generate("p")
        await client.generate("p")

        # Assert — 2 回目は primary を試さない
        assert primary.calls == 1
        assert local.calls == 2


class _FailOnceLLM(_FakeLLM):
    """先頭 ``fails`` 回だけ指定の失敗を返し、以降は通常応答する fake。

    既定 1 回。再試行 (REQUEST_RETRIES) で回復しない状況を作るときは 2 以上にする。
    """

    def __init__(self, name: str, *, error: Exception | None = None, fails: int = 1) -> None:
        super().__init__(name)
        self._first_error = error or LLMStructuredOutputError("broken")
        self._fails = fails

    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        think: bool | None = None,
    ) -> LLMResponse:
        self.calls += 1
        if self.calls <= self._fails:
            raise self._first_error
        return LLMResponse(text="ok", model=self._name)

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
        if self.calls <= self._fails:
            raise self._first_error
        return schema(label="ok")


class _RefuseOnceLLM(_FailOnceLLM):
    """1 回目だけ拒否する fake (再試行で回復する側の検証用)。"""

    def __init__(self, name: str) -> None:
        super().__init__(name, error=LLMRefusalError("refused"))


class TestModelLabelIsPerCall:
    """``model`` は「直前の応答を作った腕」。sticky にすると再生成判定が壊れる。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("structured", [False, True])
    async def test_success_after_a_fallback_is_not_labelled_fallback(
        self, structured: bool
    ) -> None:
        # Arrange — 1 回目だけ拒否される (以降は primary が応答する)
        FallbackLLMClient.reset_cooldowns()
        client = FallbackLLMClient(
            primary=_FailOnceLLM("claudecode:sonnet", fails=1 + REQUEST_RETRIES),
            fallback=_FakeLLM("gemma4:31b"),
        )

        # Act — 1 件目 (fallback) → 2 件目 (primary)
        if structured:
            await client.generate_structured("p", _Out)
            first = client.model
            await client.generate_structured("p", _Out)
        else:
            await client.generate("p")
            first = client.model
            await client.generate("p")

        # Assert — 1 件目は fallback 表記、2 件目は primary 表記に戻る。
        # 戻らないと「1 件落ちた以降の成功分」まで作り直し対象に見える
        assert first == "claudecode:sonnet→gemma4:31b"
        assert client.model == "claudecode:sonnet"


class TestFailureStreakEscalates:
    """1 件ごとの失敗も **連続すれば** 系統的な故障として cooldown する。"""

    @pytest.mark.asyncio
    async def test_streak_reaches_cooldown(self) -> None:
        # Arrange — primary が常に JSON を返せない (壊れている)
        FallbackLLMClient.reset_cooldowns()
        primary = _FakeLLM("claudecode:sonnet", error=LLMStructuredOutputError("broken"))
        local = _FakeLLM("gemma4:31b")
        client = FallbackLLMClient(primary=primary, fallback=local)

        # Act — FAILURE_STREAK 回まで試し、その後もう 1 回呼ぶ
        for _ in range(FAILURE_STREAK + 1):
            await client.generate("p")

        # Assert — streak に達した時点で cooldown に入り、以降は試さない。
        # 1 回の呼出が本試行 + 再試行を消費する
        assert primary.calls == FAILURE_STREAK * (1 + REQUEST_RETRIES)

    @pytest.mark.asyncio
    async def test_a_success_resets_the_streak(self) -> None:
        # Arrange — 1 回目だけ失敗し、以降は成功する
        FallbackLLMClient.reset_cooldowns()
        primary = _FailOnceLLM("claudecode:sonnet")
        client = FallbackLLMClient(primary=primary, fallback=_FakeLLM("gemma4:31b"))

        # Act — 失敗 1 回 → 成功 → さらに 2 回
        for _ in range(4):
            await client.generate("p")

        # Assert — 途中の成功で数え直すので cooldown に入らない
        # (1 回目が本試行 + 再試行で 2 回、以降 3 回は 1 回ずつ)
        assert primary.calls == 5


class TestPerRequestFailureIsRetriedOnce:
    """1 件ごとの失敗は揺らぎのことが多い — 落とす前に 1 回だけ投げ直す。

    拒否だけでなく、CLI が API を呼ばずに落ちる類の失敗も同じ (2026-08-28 実測)。
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("structured", [False, True])
    async def test_a_single_failure_is_recovered_by_retry(self, structured: bool) -> None:
        # Arrange — 1 回目だけ失敗 (再試行なら通る)
        FallbackLLMClient.reset_cooldowns()
        primary = _RefuseOnceLLM("claudecode:sonnet")
        local = _FakeLLM("gemma4:31b")
        client = FallbackLLMClient(primary=primary, fallback=local)

        # Act
        if structured:
            await client.generate_structured("p", _Out)
        else:
            await client.generate("p")

        # Assert — ローカルへ落ちずに primary の応答を得る
        assert local.calls == 0
        assert client.model == "claudecode:sonnet"

    @pytest.mark.asyncio
    async def test_persistent_refusal_still_falls_back(self) -> None:
        # Arrange — 何度投げても拒否される
        FallbackLLMClient.reset_cooldowns()
        primary = _FakeLLM("claudecode:sonnet", error=LLMRefusalError("no"))
        local = _FakeLLM("gemma4:31b")
        client = FallbackLLMClient(primary=primary, fallback=local)

        # Act
        await client.generate("p")

        # Assert — 粘りすぎず 1 回の再試行で諦める
        assert primary.calls == 1 + REQUEST_RETRIES
        assert local.calls == 1


class TestTransientCliFailureIsRetried:
    """CLI が API を呼ばずに落ちる失敗も再試行の対象 (拒否だけに限らない)。"""

    @pytest.mark.asyncio
    async def test_structured_failure_recovers_without_local_fallback(self) -> None:
        # Arrange — 1 回目だけ JSON が壊れる
        FallbackLLMClient.reset_cooldowns()
        primary = _FailOnceLLM("claudecode:opus", error=LLMStructuredOutputError("broken"))
        local = _FakeLLM("gemma4:31b")
        client = FallbackLLMClient(primary=primary, fallback=local)

        # Act
        await client.generate("p")

        # Assert — ローカルへ落ちずに primary の応答を得る
        assert local.calls == 0
        assert client.model == "claudecode:opus"
