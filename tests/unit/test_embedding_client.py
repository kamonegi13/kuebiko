"""src.tools.embedding_client のテスト (Phase 3b)。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.tools.embedding_client import (
    DEFAULT_EMBED_MODEL,
    EmbeddingError,
    EmbeddingModelNotFoundError,
    EmbeddingResponse,
    OllamaEmbeddingClient,
)
from src.tools.llm_client import LLMForbiddenModelError


class TestModelWhitelist:
    @pytest.mark.parametrize(
        "name",
        [
            "qwen3-embed:latest",
            "deepseek-embed",
            "bge-m3",
            "bge_large_zh",
            "internlm-embed",
            "m3e-large",
            "GLM-Embed",  # 大文字小文字無視
            "namespace/qwen-embed",  # namespace つきでも禁止
        ],
    )
    def test_rejects_chinese_models(self, name: str) -> None:
        with pytest.raises(LLMForbiddenModelError):
            OllamaEmbeddingClient(model=name)

    @pytest.mark.parametrize(
        "name",
        [
            "intfloat/multilingual-e5-large-instruct",
            "nomic-embed-text",
            "mxbai-embed-large",
        ],
    )
    def test_accepts_safe_models(self, name: str) -> None:
        # 例外が出ないこと
        OllamaEmbeddingClient(model=name)


class TestEmbed:
    @pytest.mark.asyncio
    async def test_embed_returns_response(self) -> None:
        client = OllamaEmbeddingClient(model=DEFAULT_EMBED_MODEL)
        # ollama AsyncClient.embed をモック
        mock_response = AsyncMock()
        mock_response.embeddings = [[0.1, 0.2, 0.3, 0.4]]
        client._client.embed = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]

        result = await client.embed("hello world")
        assert isinstance(result, EmbeddingResponse)
        assert result.dim == 4
        assert result.model == DEFAULT_EMBED_MODEL
        assert len(result.vector) == 4
        # 2 回目以降の dim 検証用
        assert client.dim == 4

    @pytest.mark.asyncio
    async def test_truncates_long_input(self) -> None:
        client = OllamaEmbeddingClient(model=DEFAULT_EMBED_MODEL, max_input_chars=10)
        captured: dict[str, str] = {}

        async def fake_embed(*, model: str, input: str) -> object:  # noqa: A002
            captured["input"] = input
            r = AsyncMock()
            r.embeddings = [[0.0]]
            return r

        client._client.embed = fake_embed  # type: ignore[method-assign, assignment]  # 単純化した fake bound method を差し込む
        await client.embed("a" * 100)
        assert len(captured["input"]) == 10

    @pytest.mark.asyncio
    async def test_query_prefix_only_on_query(self) -> None:
        """A1: kind='query' のときだけ prefix 前置、document は無印 (既存 embedding と整合)。"""
        client = OllamaEmbeddingClient(model=DEFAULT_EMBED_MODEL, query_prefix="query: ")
        captured: list[str] = []

        async def fake_embed(*, model: str, input: str) -> object:  # noqa: A002
            captured.append(input)
            r = AsyncMock()
            r.embeddings = [[0.0]]
            return r

        client._client.embed = fake_embed  # type: ignore[method-assign, assignment]  # 単純化した fake bound method を差し込む
        await client.embed("中国の通信事業者侵入", kind="query")
        await client.embed("記事本文テキスト", kind="document")
        await client.embed("既定は document")  # kind 省略 = document
        assert captured[0] == "query: 中国の通信事業者侵入"
        assert captured[1] == "記事本文テキスト"
        assert captured[2] == "既定は document"

    @pytest.mark.asyncio
    async def test_empty_query_prefix_noop(self) -> None:
        client = OllamaEmbeddingClient(model=DEFAULT_EMBED_MODEL, query_prefix="")
        captured: list[str] = []

        async def fake_embed(*, model: str, input: str) -> object:  # noqa: A002
            captured.append(input)
            r = AsyncMock()
            r.embeddings = [[0.0]]
            return r

        client._client.embed = fake_embed  # type: ignore[method-assign, assignment]  # 単純化した fake bound method を差し込む
        await client.embed("x", kind="query")
        assert captured[0] == "x"

    @pytest.mark.asyncio
    async def test_404_raises_model_not_found(self) -> None:
        import ollama

        client = OllamaEmbeddingClient(model="nope")

        async def fake_embed(*args: object, **kwargs: object) -> object:
            raise ollama.ResponseError("model not found", status_code=404)

        client._client.embed = fake_embed  # type: ignore[method-assign, assignment]  # 単純化した fake bound method を差し込む
        with pytest.raises(EmbeddingModelNotFoundError):
            await client.embed("x")

    @pytest.mark.asyncio
    async def test_empty_response_raises(self) -> None:
        client = OllamaEmbeddingClient(model=DEFAULT_EMBED_MODEL)
        mock_response = AsyncMock()
        mock_response.embeddings = []
        client._client.embed = AsyncMock(return_value=mock_response)  # type: ignore[method-assign]
        with pytest.raises(EmbeddingError):
            await client.embed("x")

    @pytest.mark.asyncio
    async def test_dim_mismatch_raises(self) -> None:
        client = OllamaEmbeddingClient(model=DEFAULT_EMBED_MODEL)
        # 1 回目は 3 次元
        r1 = AsyncMock()
        r1.embeddings = [[0.1, 0.2, 0.3]]
        # 2 回目は 4 次元 (異常)
        r2 = AsyncMock()
        r2.embeddings = [[0.1, 0.2, 0.3, 0.4]]
        responses = [r1, r2]

        async def fake(**kwargs: object) -> object:
            return responses.pop(0)

        client._client.embed = fake  # type: ignore[method-assign, assignment]  # 単純化した fake bound method を差し込む
        await client.embed("first")
        with pytest.raises(EmbeddingError, match="次元が不一致"):
            await client.embed("second")


class TestEmbedRetry:
    """一時障害の指数バックオフ retry (監査 2026-08-01: docstring と実装の乖離解消)。

    retry なしだと Ollama が一瞬詰まっただけでその記事の embedding が永久欠落し、
    ベクトル類似 dedup の盲点 (クロスソース再投稿) が漸増する。
    """

    @pytest.mark.asyncio
    async def test_embed_retries_transient_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import httpx

        from src.tools import embedding_client as ec

        monkeypatch.setattr(ec, "_EMBED_BACKOFF_BASE_SECONDS", 0.0)
        client = OllamaEmbeddingClient(model=DEFAULT_EMBED_MODEL)
        calls = {"n": 0}

        async def flaky(*, model: str, input: str) -> object:  # noqa: A002
            calls["n"] += 1
            if calls["n"] < 3:
                raise httpx.ConnectError("boom")
            r = AsyncMock()
            r.embeddings = [[0.0]]
            return r

        client._client.embed = flaky  # type: ignore[method-assign, assignment]  # fake bound method
        result = await client.embed("x")
        assert calls["n"] == 3
        assert result.dim == 1

    @pytest.mark.asyncio
    async def test_embed_gives_up_after_max_attempts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import httpx

        from src.tools import embedding_client as ec

        monkeypatch.setattr(ec, "_EMBED_BACKOFF_BASE_SECONDS", 0.0)
        client = OllamaEmbeddingClient(model=DEFAULT_EMBED_MODEL)
        calls = {"n": 0}

        async def always_fail(*, model: str, input: str) -> object:  # noqa: A002
            calls["n"] += 1
            raise httpx.ConnectError("boom")

        client._client.embed = always_fail  # type: ignore[method-assign, assignment]  # fake bound method
        with pytest.raises(EmbeddingError, match="接続エラー"):
            await client.embed("x")
        assert calls["n"] == 3

    @pytest.mark.asyncio
    async def test_model_not_found_does_not_retry(self) -> None:
        import ollama

        client = OllamaEmbeddingClient(model=DEFAULT_EMBED_MODEL)
        calls = {"n": 0}

        async def not_found(*, model: str, input: str) -> object:  # noqa: A002
            calls["n"] += 1
            raise ollama.ResponseError("not found", 404)

        client._client.embed = not_found  # type: ignore[method-assign, assignment]  # fake bound method
        with pytest.raises(EmbeddingModelNotFoundError):
            await client.embed("x")
        assert calls["n"] == 1
