"""外部 LLM → ローカル LLM の自動フォールバック層 (2026-07-19)。

外部経路 (Anthropic API / Claude Code サブスク bridge) はレート制限・残高不足・
bridge 停止・認証切れ等で**利用できない瞬間がある**。パイプラインの 1 step を
そこで失敗させず、当該ティアのローカル既定 (``BUILTIN_MODEL_TIERS``) に自動で
切り替えて処理を継続する。

設計:
- **可用性系の失敗のみで発動** — ``LLMForbiddenModelError`` (セキュリティゲート) は
  絶対に fallback で握り潰さない (そのまま raise)。それ以外の ``LLMError`` 系
  (接続不可 / タイムアウト / レート制限 / 構造化出力の再試行枯渇) は fallback する
- **cooldown**: 一度失敗したら ``COOLDOWN_SECONDS`` の間は外部を試さず直接ローカルへ
  (レート制限中に 15 call が毎回外部の失敗を待つ無駄を避ける)。process 内で
  primary モデル別に共有 (夜間バッチは同一 client を使い回すため run 内で有効)。
  ⭐ **拒否 (``LLMRefusalError``) は cooldown に入れない** — サービスは正常で、
  その 1 件だけ応答しない状態だから。可用性系と混ぜると 1 件の拒否が後続の
  無関係な生成を 10 分巻き添えにし、遡及が「枠切れ」と誤認して止まった
  (2026-08-27 実測)。当該 1 件だけローカルへ落として次へ進む
- **正直な記録**: ``model`` は **直前の 1 応答を作った腕**を表す
  (fallback したなら ``"<primary>→<fallback>"``)。呼出元は生成直後に読む。
  ⭐ 以前は「一度でも fallback した client は以後ずっと → 表記」だったが、
  同じ client を数百件で使い回すバッチでは **1 件の fallback 以降すべての
  成功応答まで fallback 扱い**になり、再生成の要否判定や自己調整が
  壊れた (2026-08-27 実測)。腕ごとの記録は per-call でなければ意味を持たない
- 発動は WARNING ログ (``llm_fallback_engaged``) で常に可視化する
"""

from __future__ import annotations

import time
from typing import ClassVar, TypeVar

from pydantic import BaseModel

from src.logging_config import get_logger
from src.tools.llm_client import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    MAX_STRUCTURED_ATTEMPTS,
    LLMClient,
    LLMError,
    LLMForbiddenModelError,
    LLMRefusalError,
    LLMResponse,
)

_log = get_logger(__name__)

# 失敗後に外部を再試行しない期間。レート制限 (5h 窓) には短いが、回復検知の遅れと
# 無駄な失敗待ちのバランス点として 10 分 (assessments cache 等と同じ時定数)。
COOLDOWN_SECONDS = 600.0

_T = TypeVar("_T", bound=BaseModel)


class ThinkOnClient(LLMClient):
    """think を常時 True で内側 client へ透過する wrapper (narrative ティアの外部モデル用)。

    call site は安全既定 think=False のまま — think 方針は factory (model_tiers) が
    一元所有し、この wrapper で注入する。内側が FallbackLLMClient でも、ローカルアームは
    Fallback 側が think=False にクランプするため local へ think が漏れない。
    """

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner

    @property
    def model(self) -> str:
        return self._inner.model

    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        think: bool | None = None,  # noqa: ARG002 — 方針で常時 True に上書き
    ) -> LLMResponse:
        return await self._inner.generate(
            prompt, system=system, temperature=temperature, max_tokens=max_tokens, think=True
        )

    async def generate_structured(
        self,
        prompt: str,
        schema: type[_T],
        system: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        think: bool | None = None,  # noqa: ARG002 — 方針で常時 True に上書き
        max_attempts: int = MAX_STRUCTURED_ATTEMPTS,
    ) -> _T:
        return await self._inner.generate_structured(
            prompt,
            schema,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            think=True,
            max_attempts=max_attempts,
        )


class FallbackLLMClient(LLMClient):
    """primary (外部) が失敗したら fallback (ローカル) で継続する合成 client。"""

    # primary モデル別の cooldown 期限 (monotonic)。process 内共有 — UI プロセスでは
    # request を跨いで効き、pipeline subprocess では run 内で効く。
    _cooldown_until: ClassVar[dict[str, float]] = {}

    def __init__(
        self,
        primary: LLMClient,
        fallback: LLMClient,
        cooldown_seconds: float = COOLDOWN_SECONDS,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._cooldown_seconds = cooldown_seconds
        #: 直前の応答を fallback アームが作ったか (per-call。sticky にしない)
        self._fell_back = False

    @property
    def model(self) -> str:
        if self._fell_back:
            return f"{self._primary.model}→{self._fallback.model}"
        return self._primary.model

    def _in_cooldown(self) -> bool:
        until = self._cooldown_until.get(self._primary.model, 0.0)
        return time.monotonic() < until

    def _enter_cooldown(self, error: Exception) -> None:
        self._cooldown_until[self._primary.model] = time.monotonic() + self._cooldown_seconds
        self._fell_back = True
        _log.warning(
            "llm_fallback_engaged",
            primary=self._primary.model,
            fallback=self._fallback.model,
            cooldown_seconds=self._cooldown_seconds,
            reason=str(error)[:200],
        )

    def _note_refusal(self, error: Exception) -> None:
        """拒否 1 件を記録する。**cooldown には入れない** (可用性の問題ではない)。

        ``_fell_back`` は立てる — 生成物の ``model`` 表記が
        「sonnet と言いながら中身は 31B」にならないようにするため。
        """
        self._fell_back = True
        _log.warning(
            "llm_refusal_fallback",
            primary=self._primary.model,
            fallback=self._fallback.model,
            reason=str(error)[:200],
        )

    @classmethod
    def reset_cooldowns(cls) -> None:
        """tests / 明示回復用。"""
        cls._cooldown_until.clear()

    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        think: bool | None = None,
    ) -> LLMResponse:
        if not self._in_cooldown():
            try:
                response = await self._primary.generate(
                    prompt,
                    system=system,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    think=think,
                )
                self._fell_back = False  # この応答は primary が作った
                return response
            except LLMForbiddenModelError:
                raise  # セキュリティゲートは fallback で迂回しない
            except LLMRefusalError as e:
                self._note_refusal(e)  # この 1 件だけローカルへ (cooldown はしない)
            except LLMError as e:
                self._enter_cooldown(e)
        else:
            self._fell_back = True
        # fallback アームは常にローカル Ollama (BUILTIN)。gemma は thinking で空応答化した
        # 前歴があるため、呼出元の think 指定に関わらず常に無効化する (外部 narrative の
        # think=True がフォールバック時にローカルへ漏れるのを構造的に防ぐ)。
        return await self._fallback.generate(
            prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            think=False,
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
        if not self._in_cooldown():
            try:
                parsed = await self._primary.generate_structured(
                    prompt,
                    schema,
                    system=system,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    think=think,
                    max_attempts=max_attempts,
                )
                self._fell_back = False  # この応答は primary が作った
                return parsed
            except LLMForbiddenModelError:
                raise
            except LLMRefusalError as e:
                self._note_refusal(e)  # この 1 件だけローカルへ (cooldown はしない)
            except LLMError as e:
                self._enter_cooldown(e)
        else:
            self._fell_back = True
        # generate と同じ理由でローカルアームは think 常時無効 (gemma thinking 前歴)。
        return await self._fallback.generate_structured(
            prompt,
            schema,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            think=False,
            max_attempts=max_attempts,
        )
