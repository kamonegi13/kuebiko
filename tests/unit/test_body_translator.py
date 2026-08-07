"""記事本文オンデマンド日本語訳 (src/cti/body_translator.py) のユニットテスト。

チャンク分割 (段落境界・強制分割) と translate_body (結合・空応答エラー) を検証。
"""

from __future__ import annotations

from typing import Any, TypeVar

import pytest
from pydantic import BaseModel

from src.cti.body_translator import (
    _CHUNK_MAX_CHARS,
    body_hash_for_translation,
    is_probably_japanese,
    split_for_translation,
    translate_body,
    translate_body_resumable,
)
from src.tools.llm_client import LLMClient, LLMError, LLMResponse

_T = TypeVar("_T", bound=BaseModel)


class FakeLLM(LLMClient):
    """generate 呼出を記録し、固定応答を返す fake (model property 必須)。"""

    def __init__(self, responses: list[str] | None = None) -> None:
        self.prompts: list[str] = []
        self.kwargs: list[dict[str, Any]] = []
        self._responses = responses

    @property
    def model(self) -> str:
        return "fake-model"

    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        think: bool | None = None,
    ) -> LLMResponse:
        self.prompts.append(prompt)
        self.kwargs.append({"system": system, "temperature": temperature, "think": think})
        if self._responses is not None:
            text = self._responses[len(self.prompts) - 1]
        else:
            text = f"訳{len(self.prompts)}"
        return LLMResponse(
            text=text,
            model="fake-model",
            input_tokens=1,
            output_tokens=1,
            duration_seconds=0.0,
        )

    async def generate_structured(
        self,
        prompt: str,
        schema: type[_T],
        system: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        think: bool | None = None,
        max_attempts: int = 3,
    ) -> _T:
        raise NotImplementedError


# ---------- split_for_translation ----------


def test_split_empty_returns_no_chunks() -> None:
    assert split_for_translation("") == []
    assert split_for_translation("   \n\n  ") == []


def test_split_short_text_single_chunk() -> None:
    text = "First paragraph.\n\nSecond paragraph."
    assert split_for_translation(text) == [text]


def test_split_respects_paragraph_boundaries() -> None:
    # 各段落 40 字 x 3、max 100 字 → 2 段落 + 1 段落 の 2 チャンクになる
    p = "a" * 40
    chunks = split_for_translation(f"{p}\n\n{p}\n\n{p}", max_chars=100)
    assert len(chunks) == 2
    assert chunks[0] == f"{p}\n\n{p}"
    assert chunks[1] == p


def test_split_oversize_paragraph_is_hard_split() -> None:
    # 段落 1 つが max を超える場合は強制分割され、全文が失われず保持される
    text = "x" * 250
    chunks = split_for_translation(text, max_chars=100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks) == text


def test_split_default_max_matches_body_scale() -> None:
    # 旧 body 上限 20k 字が 4-5 チャンクに収まる規模 (100k 化後も分割則は同じ)
    chunks = split_for_translation("y" * 20_000)
    assert len(chunks) <= (20_000 // _CHUNK_MAX_CHARS) + 1


# ---------- is_probably_japanese ----------


def test_japanese_body_detected() -> None:
    # security-next 型: 日本語ソースの記事 (かな比率が高い)
    text = "「ManageEngine ADAudit Plus」に深刻な脆弱性が明らかとなった。修正版が公開されている。"
    assert is_probably_japanese(text) is True


def test_english_body_not_japanese() -> None:
    assert is_probably_japanese("A serious vulnerability was found in the auditing tool.") is False


def test_chinese_and_korean_bodies_are_translatable() -> None:
    # かなを含まない中国語/韓国語は「要翻訳」側に落ちる (中→日 / 韓→日は翻訳したい)
    assert is_probably_japanese("发现了一个严重的漏洞，攻击者可以远程执行代码。") is False
    assert (
        is_probably_japanese(
            "심각한 취약점이 발견되었습니다. 공격자가 원격으로 코드를 실행할 수 있습니다."
        )
        is False
    )


def test_english_with_quoted_japanese_name_not_japanese() -> None:
    # 英文中の日本語引用 (かな比率 5% 未満) は英文扱い
    text = (
        "The Japanese CERT (known as ジェイピーサート) published a lengthy advisory about the "
        "vulnerability affecting multiple enterprise products across many sectors worldwide. "
        "Administrators are urged to apply the vendor patches immediately and review logs."
    )
    assert is_probably_japanese(text) is False


def test_empty_body_not_japanese() -> None:
    assert is_probably_japanese("   ") is False


# ---------- translate_body ----------


async def test_translate_body_single_chunk() -> None:
    llm = FakeLLM(responses=["これは訳文です。"])
    out = await translate_body(llm, "This is the original body.")
    assert out == "これは訳文です。"
    assert len(llm.prompts) == 1
    # 翻訳は think=False 固定 (Gemma thinking 空応答対策) + 低 temperature
    assert llm.kwargs[0]["think"] is False
    assert llm.kwargs[0]["temperature"] == 0.2
    assert llm.kwargs[0]["system"] is not None


async def test_translate_body_joins_chunks_in_order() -> None:
    llm = FakeLLM()
    p = "b" * 4000
    out = await translate_body(llm, f"{p}\n\n{p}\n\n{p}")
    # 5000 字 max → 複数チャンクが順序どおり空行結合される
    assert len(llm.prompts) >= 2
    assert out == "\n\n".join(f"訳{i + 1}" for i in range(len(llm.prompts)))


async def test_translate_body_empty_text_raises() -> None:
    with pytest.raises(LLMError):
        await translate_body(FakeLLM(), "   ")


async def test_translate_body_empty_response_raises() -> None:
    # 空応答は部分訳キャッシュを作らずエラーにする (all-or-nothing)
    llm = FakeLLM(responses=[""])
    with pytest.raises(LLMError):
        await translate_body(llm, "Some body text.")


# ---------- translate_body_resumable ----------


class MemoryChunkStore:
    """ChunkStore の in-memory 実装 (hash 不一致の無効化を repo 実装と同義に再現)。"""

    def __init__(self) -> None:
        self.rows: dict[int, tuple[str, str]] = {}  # seq -> (body_hash, text)

    def get_body_ja_chunks(self, article_id: str, body_hash: str) -> dict[int, str]:
        if self.rows and any(h != body_hash for h, _ in self.rows.values()):
            self.rows.clear()
            return {}
        return {seq: text for seq, (_, text) in self.rows.items()}

    def save_body_ja_chunk(
        self, article_id: str, seq: int, total: int, body_hash: str, text: str
    ) -> None:
        self.rows.setdefault(seq, (body_hash, text))


# 4000 字 x 3 段落 → _CHUNK_MAX_CHARS=5000 では 1 段落 = 1 チャンクの 3 チャンク
_THREE_CHUNK_BODY = "\n\n".join("b" * 4000 for _ in range(3))


async def test_resumable_completes_and_saves_all_chunks() -> None:
    store = MemoryChunkStore()
    llm = FakeLLM()
    progress = await translate_body_resumable(llm, _THREE_CHUNK_BODY, article_id="a1", store=store)
    assert progress.is_complete
    assert progress.text == "訳1\n\n訳2\n\n訳3"
    assert progress.partial_text == progress.text
    assert (progress.done_chunks, progress.total_chunks) == (3, 3)
    assert len(store.rows) == 3


async def test_resumable_resumes_after_midway_llm_failure() -> None:
    store = MemoryChunkStore()
    # chunk0 は成功、chunk1 で空応答 → LLMError。ただし chunk0 は store に確定済み
    with pytest.raises(LLMError):
        await translate_body_resumable(
            FakeLLM(responses=["訳1", ""]),
            _THREE_CHUNK_BODY,
            article_id="a1",
            store=store,
        )
    assert set(store.rows) == {0}

    # 再試行は未訳チャンク (1, 2) だけを処理して完了する
    retry_llm = FakeLLM(responses=["訳2b", "訳3b"])
    progress = await translate_body_resumable(
        retry_llm, _THREE_CHUNK_BODY, article_id="a1", store=store
    )
    assert progress.is_complete
    assert progress.text == "訳1\n\n訳2b\n\n訳3b"
    assert len(retry_llm.prompts) == 2


async def test_resumable_deadline_stops_at_chunk_boundary() -> None:
    store = MemoryChunkStore()
    # clock: started=0 / chunk0 判定=0 (<50 → 翻訳) / chunk1 判定=100 (>=50 → 中断)
    ticks = iter([0.0, 0.0, 100.0])
    progress = await translate_body_resumable(
        FakeLLM(),
        _THREE_CHUNK_BODY,
        article_id="a1",
        store=store,
        deadline_seconds=50.0,
        clock=lambda: next(ticks),
    )
    assert not progress.is_complete
    assert progress.text is None
    assert (progress.done_chunks, progress.total_chunks) == (1, 3)
    assert progress.partial_text == "訳1"
    assert set(store.rows) == {0}


async def test_resumable_stale_hash_discards_old_chunks() -> None:
    store = MemoryChunkStore()
    store.rows[0] = ("old-hash", "古い訳")
    progress = await translate_body_resumable(
        FakeLLM(), _THREE_CHUNK_BODY, article_id="a1", store=store
    )
    # 本文差し替え (hash 不一致) → 古い部分訳は捨てて全チャンク訳し直し
    assert progress.is_complete
    assert progress.text == "訳1\n\n訳2\n\n訳3"


async def test_resumable_partial_text_is_contiguous_prefix_only() -> None:
    store = MemoryChunkStore()
    h = body_hash_for_translation(_THREE_CHUNK_BODY)
    # 非連続キャッシュ (chunk1 のみ) — 表示用 partial_text は先頭連続分のみ
    store.rows[1] = (h, "訳B")
    ticks = iter([0.0, 100.0])  # 即時 deadline → chunk0 を訳さず中断
    progress = await translate_body_resumable(
        FakeLLM(),
        _THREE_CHUNK_BODY,
        article_id="a1",
        store=store,
        deadline_seconds=50.0,
        clock=lambda: next(ticks),
    )
    assert not progress.is_complete
    assert progress.done_chunks == 1
    assert progress.partial_text == ""
