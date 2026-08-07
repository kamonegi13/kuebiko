"""Ollama Embedding クライアント (Phase 3b)。

意味的重複排除 (英語 vs 日本語の同一インシデント検出) のために、
Ollama の embed API を呼び出して dense vector を返す。

設計方針:
- 中国系 embedding モデル (bge-, m3e, qwen3-embed 等) は LLM クライアントと
  同一の ``FORBIDDEN_MODEL_PREFIXES`` ホワイトリストで防御 (CLAUDE.md §4)
- モデル未取得 (404) は ``EmbeddingModelNotFoundError`` で上位に通知
- ネットワーク失敗時はリトライ (指数バックオフ、最大 3 回)
- ``LLMForbiddenModelError`` を流用 (両クライアントで共通の例外を出す)
- 入力テキストはタイトル + 本文先頭 ~2000 文字に制限 (LLM 側と同じ機密境界)

CLAUDE.md §4 の推奨は ``intfloat/multilingual-e5-large-instruct`` (Microsoft)
だが、Ollama のモデル名は環境次第なので ``.env`` の ``OLLAMA_EMBED_MODEL``
で上書き可能にする。
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Literal

import httpx
import ollama
from pydantic import BaseModel, ConfigDict

from src.logging_config import get_logger
from src.tools.llm_client import (
    DEFAULT_BASE_URL,
    validate_model_name,
)

_log = get_logger(__name__)

DEFAULT_EMBED_MODEL = "intfloat/multilingual-e5-large-instruct"
DEFAULT_EMBED_TIMEOUT_SECONDS = 120.0
# 1 入力あたりの最大文字数 (品質と速度のバランス)
DEFAULT_MAX_INPUT_CHARS = 2000
# Phase 検索改善 A1: asymmetric retrieval の query prefix。snowflake-arctic-embed2 /
# multilingual-e5 は **query 側のみ** "query: " を前置する仕様 (document は無印)。
# これが無いと query/document が同一視され retrieval 品質が落ちる (cosine 圧縮の主因)。
# document は無印のため既存 embedding を再生成せず query 側だけで即改善できる。
DEFAULT_QUERY_PREFIX = "query: "
# 一時障害 (接続断/タイムアウト/5xx) の retry 上限と指数バックオフ基底 (監査 2026-08-01:
# docstring の「最大 3 回」が未実装で、Ollama が一瞬詰まるとその記事の embedding が
# 永久欠落 → ベクトル類似 dedup の盲点が漸増していた)。404 (モデル不在) は retry しない。
_EMBED_MAX_ATTEMPTS = 3
_EMBED_BACKOFF_BASE_SECONDS = 0.5

EmbedKind = Literal["query", "document"]


class EmbeddingError(Exception):
    """Embedding 関連エラーのベース。"""


class EmbeddingModelNotFoundError(EmbeddingError):
    """指定 embedding モデルが Ollama サーバに存在しない。"""


class EmbeddingResponse(BaseModel):
    """1 件の embedding 推論結果。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    vector: tuple[float, ...]  # frozen のため list ではなく tuple
    model: str
    dim: int
    duration_seconds: float = 0.0


class EmbeddingClient(ABC):
    """Embedding クライアント抽象。テストで差し替えできるよう ABC にする。"""

    @abstractmethod
    async def embed(self, text: str, *, kind: EmbedKind = "document") -> EmbeddingResponse:
        """テキストを 1 つの dense vector に変換する。

        ``kind="query"`` のとき asymmetric retrieval の query prefix を前置する
        (document 側は無印)。検索クエリは ``kind="query"``、記事本文は ``"document"``。
        """

    @property
    @abstractmethod
    def dim(self) -> int | None:
        """既知ならベクトル次元数。未確定なら None。"""

    @property
    @abstractmethod
    def model(self) -> str:
        """使用中のモデル名。"""


class OllamaEmbeddingClient(EmbeddingClient):
    """Ollama 経由で embedding を取得する具象クライアント。"""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_EMBED_MODEL,
        timeout_seconds: float = DEFAULT_EMBED_TIMEOUT_SECONDS,
        max_input_chars: int = DEFAULT_MAX_INPUT_CHARS,
        query_prefix: str = DEFAULT_QUERY_PREFIX,
    ) -> None:
        _check_model_whitelist(model)
        self._client = ollama.AsyncClient(host=base_url, timeout=timeout_seconds)
        self._model = model
        self._max_input_chars = max_input_chars
        self._query_prefix = query_prefix
        self._dim: int | None = None  # 初回 embed 時に確定

    @property
    def dim(self) -> int | None:
        return self._dim

    @property
    def model(self) -> str:
        return self._model

    async def embed(self, text: str, *, kind: EmbedKind = "document") -> EmbeddingResponse:
        # A1: query 側のみ prefix を前置 (document は無印)。truncate 前に付ける。
        prepared = f"{self._query_prefix}{text}" if kind == "query" and self._query_prefix else text
        truncated = prepared[: self._max_input_chars]
        started = time.perf_counter()
        response = None
        for attempt in range(1, _EMBED_MAX_ATTEMPTS + 1):
            try:
                response = await self._client.embed(model=self._model, input=truncated)
                break
            except ollama.ResponseError as e:
                if e.status_code == 404:
                    raise EmbeddingModelNotFoundError(
                        f"embedding モデル '{self._model}' が Ollama に存在しません",
                    ) from e
                if attempt >= _EMBED_MAX_ATTEMPTS:
                    raise EmbeddingError(f"Ollama embed エラー: {e}") from e
                transient: Exception = e
            except (httpx.HTTPError, ConnectionError) as e:
                if attempt >= _EMBED_MAX_ATTEMPTS:
                    raise EmbeddingError(f"Ollama 接続エラー: {e}") from e
                transient = e
            _log.warning(
                "embedding_retry",
                attempt=attempt,
                max_attempts=_EMBED_MAX_ATTEMPTS,
                error=str(transient)[:120],
            )
            await asyncio.sleep(_EMBED_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
        assert response is not None  # for 文は break か raise でしか抜けない

        duration = time.perf_counter() - started

        # response.embeddings は list[list[float]] (バッチ対応)
        embeddings = response.embeddings
        if not embeddings:
            raise EmbeddingError("Ollama が空の embedding を返した")
        vector = tuple(float(x) for x in embeddings[0])
        if self._dim is None:
            self._dim = len(vector)
        elif len(vector) != self._dim:
            raise EmbeddingError(
                f"embedding 次元が不一致: 期待 {self._dim}, 実際 {len(vector)}",
            )

        _log.info(
            "embedding_completed",
            model=self._model,
            dim=len(vector),
            input_chars=len(truncated),
            duration_seconds=round(duration, 3),
        )
        return EmbeddingResponse(
            vector=vector,
            model=self._model,
            dim=len(vector),
            duration_seconds=duration,
        )


def _check_model_whitelist(model: str) -> None:
    """中国系 embedding モデル名を弾く。CLAUDE.md §4 のサプライチェーンリスク対応。

    LLM クライアントと同一の ``validate_model_name`` に委譲し、防御ロジックを一元化する
    (full 名 + bare 名 + colon-embedded を網羅。両クライアントで非対称だった穴を解消)。
    """
    validate_model_name(model)
