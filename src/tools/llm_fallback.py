"""外部 LLM → ローカル LLM の自動フォールバック層 (2026-07-19)。

外部経路 (Anthropic API / Claude Code サブスク bridge) はレート制限・残高不足・
bridge 停止・認証切れ等で**利用できない瞬間がある**。パイプラインの 1 step を
そこで失敗させず、当該ティアのローカル既定 (``BUILTIN_MODEL_TIERS``) に自動で
切り替えて処理を継続する。

設計:
- **可用性系の失敗のみで発動** — ``LLMForbiddenModelError`` (セキュリティゲート) は
  絶対に fallback で握り潰さない (そのまま raise)。それ以外の ``LLMError`` 系
  (接続不可 / タイムアウト / レート制限 / 構造化出力の再試行枯渇) は fallback する
- **cooldown**: 外部が使えないときは ``COOLDOWN_SECONDS`` の間は試さず直接ローカルへ
  (レート制限中に 15 call が毎回外部の失敗を待つ無駄を避ける)。process 内で
  primary モデル別に共有 (夜間バッチは同一 client を使い回すため run 内で有効)。
  ⭐ **入れる条件は失敗の種類で分ける**:
    - 到達不能 (``LLMTimeoutError`` / ``LLMConnectionError``) = 経路の問題 → 即 cooldown
    - **その 1 件だけの失敗** (拒否 / 構造化出力の失敗 / CLI の単発エラー) は
      当該 1 件をローカルへ落とすだけで cooldown しない。サービスは正常だから。
      ただし ``FAILURE_STREAK`` 回**連続**したら系統的な故障とみなして cooldown する
  混ぜていた時期は 1 件の拒否や 1 本の JSON 崩れが後続 10 分の生成を巻き添えにし、
  バッチが「枠切れ」と誤認して止まった (2026-08-27 に 2 度)
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
from collections.abc import Awaitable, Callable
from typing import ClassVar, TypeVar

from pydantic import BaseModel

from src.logging_config import get_logger
from src.tools.llm_client import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    MAX_STRUCTURED_ATTEMPTS,
    LLMClient,
    LLMConnectionError,
    LLMError,
    LLMForbiddenModelError,
    LLMResponse,
    LLMTimeoutError,
)

_log = get_logger(__name__)

# 失敗後に外部を再試行しない期間。レート制限 (5h 窓) には短いが、回復検知の遅れと
# 無駄な失敗待ちのバランス点として 10 分 (assessments cache 等と同じ時定数)。
COOLDOWN_SECONDS = 600.0

#: 1 件ごとの失敗が何回連続したら「系統的な故障」とみなすか。
#: 1 回で cooldown すると 1 本の悪い記事が経路全体を止める。無制限に許すと
#: 本当に壊れているとき毎回外部の失敗を待つ — その間を取る。
FAILURE_STREAK = 3

#: 1 件ごとの失敗は**揺らぎであることが多い** (2026-08-28 実測: 拒否された記事を
#: 再投入すると 5/5 成功。CLI が API を呼ばずに落ちる stop_sequence も、同じ入力を
#: 投げ直すと 3/3 成功した)。落とす前に 1 回だけ投げ直す。
#: 粘りすぎないよう 1 回まで — 通らなければローカルへ渡す。
REQUEST_RETRIES = 1

_T = TypeVar("_T", bound=BaseModel)
_R = TypeVar("_R")


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
    #: primary モデル別の「1 件ごとの失敗」連続回数 (成功で 0 に戻る)
    _failure_streak: ClassVar[dict[str, int]] = {}

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
        self._failure_streak[self._primary.model] = 0
        self._fell_back = True
        _log.warning(
            "llm_fallback_engaged",
            primary=self._primary.model,
            fallback=self._fallback.model,
            cooldown_seconds=self._cooldown_seconds,
            reason=str(error)[:200],
        )

    async def _retry_once(self, attempt: Callable[[], Awaitable[_R]]) -> _R | None:
        """1 件ごとの失敗で **同じ内容を 1 回だけ**すぐ投げ直す。

        拒否も、CLI が API を呼ばずに落ちる類の失敗も、実測では**同じ入力を
        投げ直すと通る**ことが多い。ここで拾えればローカルへ落とさずに済む。
        再び失敗したら諦めて呼出元の fallback に委ねる (無限に粘ると 1 件のために
        経路を占有する)。到達不能系はここへ来ない — 呼出元で即 cooldown する。
        """
        for _ in range(REQUEST_RETRIES):
            try:
                result = await attempt()
            except LLMError:
                continue
            self._fell_back = False
            self._failure_streak[self._primary.model] = 0
            _log.info("llm_retry_succeeded", primary=self._primary.model)
            return result
        return None

    def _note_per_request_failure(self, error: Exception) -> None:
        """1 件だけの失敗を記録する。**単発なら cooldown には入れない**。

        連続して ``FAILURE_STREAK`` 回起きたら、もう「たまたま悪い入力」では
        説明できないので系統的な故障として cooldown へ移す。
        """
        self._fell_back = True
        streak = self._failure_streak.get(self._primary.model, 0) + 1
        self._failure_streak[self._primary.model] = streak
        if streak >= FAILURE_STREAK:
            self._enter_cooldown(error)
            return
        _log.warning(
            "llm_request_failed_fallback",
            primary=self._primary.model,
            fallback=self._fallback.model,
            streak=streak,
            reason=str(error)[:200],
        )

    @classmethod
    def reset_cooldowns(cls) -> None:
        """tests / 明示回復用。"""
        cls._cooldown_until.clear()
        cls._failure_streak.clear()

    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        think: bool | None = None,
    ) -> LLMResponse:
        if not self._in_cooldown():

            def attempt() -> Awaitable[LLMResponse]:
                return self._primary.generate(
                    prompt,
                    system=system,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    think=think,
                )

            try:
                response = await attempt()
                self._fell_back = False  # この応答は primary が作った
                self._failure_streak[self._primary.model] = 0
                return response
            except LLMForbiddenModelError:
                raise  # セキュリティゲートは fallback で迂回しない
            except (LLMTimeoutError, LLMConnectionError) as e:
                self._enter_cooldown(e)  # 経路が使えない → しばらく試さない
            except LLMError as e:
                # 1 件ごとの失敗 (拒否 / JSON 崩れ / CLI の単発エラー)。
                # 揺らぎのことが多いので 1 回だけ投げ直してから諦める。
                retried = await self._retry_once(attempt)
                if retried is not None:
                    return retried
                self._note_per_request_failure(e)
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

            def attempt() -> Awaitable[_T]:
                return self._primary.generate_structured(
                    prompt,
                    schema,
                    system=system,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    think=think,
                    max_attempts=max_attempts,
                )

            try:
                parsed = await attempt()
                self._fell_back = False  # この応答は primary が作った
                self._failure_streak[self._primary.model] = 0
                return parsed
            except LLMForbiddenModelError:
                raise
            except (LLMTimeoutError, LLMConnectionError) as e:
                self._enter_cooldown(e)  # 経路が使えない → しばらく試さない
            except LLMError as e:
                # 1 件ごとの失敗 (拒否 / JSON 崩れ / CLI の単発エラー)。
                # 揺らぎのことが多いので 1 回だけ投げ直してから諦める。
                retried = await self._retry_once(attempt)
                if retried is not None:
                    return retried
                self._note_per_request_failure(e)
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
