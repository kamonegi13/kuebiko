"""Ollama LLM クライアント (Step 6)。

抽象 ``LLMClient`` (ABC) + 具象 ``OllamaClient`` のレイヤ構成。
将来 LLM プロバイダを切り替える場合は ``LLMClient`` のサブクラスを追加する。
Phase 1 では Ollama のみ使用 (CLAUDE.md §2)。

中国系モデル (Qwen / DeepSeek 等) は ``OllamaClient.__init__`` で
モデル名ホワイトリスト検証によって起動段階で弾く (CLAUDE.md §4)。
同一 Ollama サーバ上に他用途で同居していても、設定ミスやタイポで
誤って中華系モデルへルーティングされる事故をコード側で防ぐ。

Phase 1 着手時の暫定: Gemma 4 31B が Ollama に未公開の間は
``gemma3:27b`` でも代替可。許容済み。
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from typing import Any, Protocol, TypeVar

import httpx
import ollama
from pydantic import BaseModel, ConfigDict, ValidationError

from src.logging_config import get_logger

_log = get_logger(__name__)

# ---------- 定数 ----------

# CLAUDE.md §4 で禁止指定された中華系 LLM/Embedding のモデル名プレフィクス。
# 大文字小文字を無視して先頭一致 / ``:`` 区切り後一致で検出する。
# この list は **コード所有のセキュリティ基盤** — config 化しない (UI で編集可能にすると
# §4 の防御自体を無効化できてしまう)。per-tier のモデル選択のみ config で許容し、
# 選択はこの denylist で制約する (model_tiers.py の3層防御の最終防御 = 構築時検証)。
FORBIDDEN_MODEL_PREFIXES: tuple[str, ...] = (
    "qwen",
    "deepseek",
    "yi:",
    "yi-",
    "glm",
    "chatglm",
    "internlm",
    "m3e",
    "bge-",
    "bge_",
    # 2026-07-08 追加: 主要な中華系 LLM ファミリを網羅 (dropdown/保存/構築の3層で弾く)。
    "baichuan",
    "ernie",
    "hunyuan",
    "minimax",
    "moonshot",
    "kimi",
    "skywork",
    "telechat",
    "xverse",
)

DEFAULT_MODEL = "gemma4:31b"
DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_TIMEOUT_SECONDS = 300.0
DEFAULT_TEMPERATURE = 0.3
DEFAULT_MAX_TOKENS = 4096

# Phase 5P: structured output で JSON 崩れ / schema 不正に出くわした際の最大試行回数。
# 1 回目失敗 + 1 回再試行 = 2 回。各試行は full timeout を使う。
MAX_STRUCTURED_ATTEMPTS = 2


def _repair_truncated_json(text: str) -> dict[str, object] | None:
    """末尾が切断された JSON を閉じて parse を試みる (救済)。

    Ollama の structured output が稀に文字列途中で停止し unterminated JSON を返す
    (例: ``"summary": "...電子戦（GP`` で停止)。開いている文字列と ``{ [`` を末尾で
    閉じて ``json.loads`` を再試行する。成功すれば dict を返す。切断された末尾の
    フィールドは欠落するが、先頭の必須フィールド (title_ja / importance / category /
    summary) は復元でき、article を完全 loss せず degraded で通せる。
    """
    start = text.find("{")
    if start < 0:
        return None
    s = text[start:]
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in s:
        if esc:
            esc = False
            continue
        if ch == "\\" and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in "{[":
            stack.append(ch)
        elif ch == "}" and stack and stack[-1] == "{" or ch == "]" and stack and stack[-1] == "[":
            stack.pop()
    repaired = s.rstrip()
    if in_str:
        repaired += '"'  # 開いた文字列を閉じる
    repaired = repaired.rstrip().rstrip(",")  # 末尾の余分なカンマを除去
    for ch in reversed(stack):
        repaired += "}" if ch == "{" else "]"
    try:
        obj = json.loads(repaired)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


# ---------- LLMResponse ----------


class LLMResponse(BaseModel):
    """LLM 推論 1 回分の結果 (frozen)。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    duration_seconds: float = 0.0


# ---------- 例外階層 ----------


class LLMError(Exception):
    """LLM 関連エラーのベース。"""


class LLMModelNotFoundError(LLMError):
    """指定モデルが Ollama サーバに存在しない (404)。"""


class LLMTimeoutError(LLMError):
    """推論がタイムアウト。"""


class LLMConnectionError(LLMError):
    """Ollama サーバに接続できない (DNS / TCP / 接続拒否)。"""


class LLMStructuredOutputError(LLMError):
    """JSON 出力の構文不正、または指定スキーマで検証失敗。"""


class LLMForbiddenModelError(LLMError):
    """ポリシーで使用を禁じられたモデル (CLAUDE.md §4)。"""


# ---------- 消費記録 callback ----------


class UsageRecorder(Protocol):
    """トークン消費の記録 callback (llm_usage テーブル、モデルタブの消費表示用)。

    cache_read_tokens / cost_usd は provider が自己申告できる場合のみ渡す
    (claudecode=bridge が CLI から取得。他 provider は省略で 0)。
    実装は fail-safe であること — 記録失敗で LLM 呼出を壊さない。
    """

    def __call__(
        self,
        input_tokens: int,
        output_tokens: int,
        duration_ms: int,
        cache_read_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None: ...


# ---------- 抽象 LLMClient ----------


_T = TypeVar("_T", bound=BaseModel)


class LLMClient(ABC):
    """LLM クライアントの抽象クラス。プロバイダ非依存のインタフェース。"""

    @property
    @abstractmethod
    def model(self) -> str:
        """使用中モデル名 (ログ / DB 記録用)。

        監査 2026-07-05 P6: spotlight 等が llm.model を参照するが抽象契約に無く、
        実装差し替えで実 crash しうる型契約の穴だった。
        """

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        think: bool | None = None,
    ) -> LLMResponse:
        """自由形式テキスト生成。

        ``think``:
            ``True``  → thinking モード ON (Gemma 4 等で多段推論を有効)
            ``False`` → thinking モード OFF (出力直行、要約等の単純タスク向け)
            ``None``  → 既定 (Ollama 側のモデル既定に従う)
        """

    @abstractmethod
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
        """JSON 出力を強制し、指定 pydantic スキーマにパースして返す。

        ``think`` パラメータについては ``generate`` を参照。
        ``max_attempts=1`` で JSON 崩れ時の盲目リトライを無効化 (重い呼出向け)。
        """


# ---------- ホワイトリスト検証 ----------


def validate_model_name(model: str) -> None:
    """中国系モデル名を起動段階で弾く (CLAUDE.md §4)。

    例: ``qwen3:7b``, ``deepseek-coder:6.7b``, ``yi:34b``, ``glm-4`` 等。

    Raises:
        LLMForbiddenModelError: 禁止プレフィクスにマッチした場合。
    """
    name_lower = model.lower().strip()
    if not name_lower:
        raise LLMForbiddenModelError("モデル名が空")
    # ``namespace/model:tag`` の双方の形を捕捉する:
    #  - full 名: ``model:qwen3`` のような colon-embedded を ``:prefix`` で捕捉
    #  - bare 名 (最後の ``/`` 以降): ``hf.co/Qwen/Qwen2.5-...`` / ``BAAI/bge-...`` のように
    #    Ollama の HuggingFace 名前空間経由で渡される中国系モデルを捕捉。bare を見ないと
    #    namespace 付きモデルが §4 の防御をすり抜ける (回帰テスト: test_llm_client)。
    bare = name_lower.split("/")[-1]
    for prefix in FORBIDDEN_MODEL_PREFIXES:
        for candidate in (name_lower, bare):
            if candidate.startswith(prefix) or f":{prefix}" in candidate:
                raise LLMForbiddenModelError(
                    f"禁止系モデル: {model!r} (CLAUDE.md §4 により中国系 LLM/Embedding は使用不可)",
                )


def is_model_allowed(model: str) -> bool:
    """``validate_model_name`` の非 raising 版 (許可 = True / 禁止・空 = False)。

    UI の model dropdown 生成 (中華系を選択肢から除外) など、例外を投げたくない
    上流フィルタで使う。denylist (``FORBIDDEN_MODEL_PREFIXES``) は
    ``validate_model_name`` と共有する SSoT のため両者は必ず一致する。
    """
    try:
        validate_model_name(model)
    except LLMForbiddenModelError:
        return False
    return True


# ---------- OllamaClient ----------


class OllamaClient(LLMClient):
    """Ollama サーバへの async クライアント (公式 SDK ベース)。

    OpenAI 互換 API は使わず、ollama 公式 SDK の ``AsyncClient`` 経由で
    アクセスする。テスト時には ``client`` 引数に ``transport=`` を仕込んだ
    ``ollama.AsyncClient`` を渡すことで実ネットワークを切り離せる。
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        client: ollama.AsyncClient | None = None,
        num_ctx: int | None = None,
    ) -> None:
        validate_model_name(model)
        self._base_url = base_url
        self._model = model
        self._timeout_seconds = timeout_seconds
        # num_ctx は None の間 Ollama 既定に委譲 (現挙動)。明示時のみ options に載せる。
        self._num_ctx = num_ctx
        if client is None:
            self._client = ollama.AsyncClient(host=base_url, timeout=timeout_seconds)
        else:
            self._client = client

    @property
    def model(self) -> str:
        return self._model

    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        think: bool | None = None,
    ) -> LLMResponse:
        return await self._chat(
            prompt=prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            format_schema=None,
            think=think,
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
        """構造化出力を取得する。Phase 5P: JSON 崩れ / schema 不正で 1 回だけリトライ。

        Ollama の散発的な JSON 崩れを吸収するため、既定で最大 ``MAX_STRUCTURED_ATTEMPTS``
        (=2) 回まで同じ prompt で再試行する。各試行は full timeout を使う
        (リトライで時間が短くなる副作用を避ける)。

        ``max_attempts=1`` で盲目リトライを無効化できる — **1 呼出が数百秒級の重い経路
        (増分 ACH 等) では、同条件リトライは切断をほぼ再現して時間だけ倍加する**
        (2026-07-04 実測: 800s×2 で pipeline 1800s を突破)。呼出側に翌 run 回復の
        仕組みがあるなら skip が正しい。
        """
        schema_dict = schema.model_json_schema()
        last_error: LLMStructuredOutputError | None = None
        attempts = max(1, max_attempts)
        for attempt in range(1, attempts + 1):
            resp = await self._chat(
                prompt=prompt,
                system=system,
                temperature=temperature,
                max_tokens=max_tokens,
                format_schema=schema_dict,
                think=think,
            )
            try:
                data = json.loads(resp.text)
            except json.JSONDecodeError as e:
                # Phase B: 末尾切断 JSON (Ollama structured output の散発的 truncation) を
                # 救済する。閉じ括弧を補って parse し schema 検証が通れば degraded で採用
                # (retry は同条件で同じ truncation を再現しがちなため、salvage を先に試す)。
                salvaged = _repair_truncated_json(resp.text)
                if salvaged is not None:
                    try:
                        obj = schema.model_validate(salvaged)
                        _log.warning(
                            "llm_structured_salvaged",
                            attempt=attempt,
                            preview=resp.text[:80],
                        )
                        return obj
                    except ValidationError:
                        pass  # salvage 後も schema 不正 → 通常の retry/fail へ
                last_error = LLMStructuredOutputError(
                    f"JSON パース失敗: {e}; 出力先頭 200: {resp.text[:200]!r}",
                )
                if attempt < attempts:
                    _log.warning(
                        "llm_structured_retry",
                        attempt=attempt,
                        reason="json_decode_error",
                        preview=resp.text[:80],
                    )
                    continue
                raise last_error from e
            try:
                return schema.model_validate(data)
            except ValidationError as e:
                last_error = LLMStructuredOutputError(
                    f"スキーマ検証失敗: {e}; 出力先頭 200: {resp.text[:200]!r}",
                )
                if attempt < attempts:
                    _log.warning(
                        "llm_structured_retry",
                        attempt=attempt,
                        reason="schema_validation_error",
                        preview=resp.text[:80],
                    )
                    continue
                raise last_error from e
        # 到達不能 (ループ内で必ず return か raise する)
        assert last_error is not None
        raise last_error

    async def _chat(
        self,
        *,
        prompt: str,
        system: str | None,
        temperature: float,
        max_tokens: int,
        format_schema: dict[str, Any] | None,
        think: bool | None = None,
    ) -> LLMResponse:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        options: dict[str, Any] = {"temperature": temperature, "num_predict": max_tokens}
        if self._num_ctx is not None:
            options["num_ctx"] = self._num_ctx

        # プロンプト本文は DEBUG レベルのみ。INFO には文字数のみ出す。
        _log.debug(
            "ollama_request_detail",
            model=self._model,
            system_chars=len(system or ""),
            prompt_chars=len(prompt),
        )

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "options": options,
        }
        if format_schema is not None:
            kwargs["format"] = format_schema
        if think is not None:
            kwargs["think"] = think

        start = time.monotonic()
        try:
            response = await self._client.chat(**kwargs)
        except ollama.ResponseError as e:
            if getattr(e, "status_code", None) == 404:
                raise LLMModelNotFoundError(
                    f"モデル {self._model!r} が Ollama に存在しません: {e}",
                ) from e
            raise LLMError(f"Ollama API エラー: {e}") from e
        except (TimeoutError, httpx.TimeoutException) as e:
            raise LLMTimeoutError(
                f"Ollama 推論タイムアウト ({self._timeout_seconds}s): {e}",
            ) from e
        except (httpx.ConnectError, ConnectionError) as e:
            # ollama 公式 SDK は httpx.ConnectError を組み込み
            # ConnectionError に変換して再 raise するため、両方拾う。
            # ConnectionRefusedError は ConnectionError のサブクラスのため包含。
            raise LLMConnectionError(
                f"Ollama サーバに接続できません ({self._base_url}): {e}",
            ) from e
        except httpx.RequestError as e:
            raise LLMConnectionError(f"Ollama リクエストエラー: {e}") from e

        duration = time.monotonic() - start
        text = _resp_message_content(response)
        prompt_eval = int(_get(response, "prompt_eval_count") or 0)
        eval_count = int(_get(response, "eval_count") or 0)

        _log.info(
            "ollama_response",
            model=self._model,
            duration_seconds=round(duration, 3),
            input_tokens=prompt_eval,
            output_tokens=eval_count,
            output_chars=len(text),
            structured=format_schema is not None,
        )

        return LLMResponse(
            text=text,
            model=self._model,
            input_tokens=prompt_eval,
            output_tokens=eval_count,
            duration_seconds=duration,
        )


# ---------- helpers (ollama レスポンスのバージョン互換) ----------


def _get(obj: object, name: str) -> Any:
    """object / dict のどちらでも値を取得する。"""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _resp_message_content(response: object) -> str:
    """``response.message.content`` を安全に取り出す。"""
    msg = _get(response, "message")
    if msg is None:
        return ""
    content = _get(msg, "content")
    return str(content) if content else ""
