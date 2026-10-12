"""記事本文オンデマンド日本語訳 (src/cti/body_translator.py) のユニットテスト。

チャンク分割 (段落境界・強制分割) と translate_body (結合・空応答エラー) を検証。
"""

from __future__ import annotations

from typing import Any, TypeVar

import pytest
import structlog
from pydantic import BaseModel

from src.cti.body_translator import (
    _CHUNK_MAX_CHARS,
    TranslationGarbledError,
    body_hash_for_translation,
    broken_translation_reason,
    fidelity_gate_mode,
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


# ---------- 壊れた訳の関門 (2026-10-02) ----------

_SRC = (
    "South Africa's Air Traffic and Navigation Services (ATNS) operator suffered a severe "
    "cyberattack targeting operational technology environments."
)


def test_json_mapping_output_is_broken() -> None:
    out = '{"Start your day with intelligence.": "一日をインテリジェンスとともに。"}\n\n訳文'
    assert broken_translation_reason(_SRC, out) == "JSON の形が混ざっている"


def test_source_echo_is_broken() -> None:
    out = _SRC + "\n\n南アフリカの ATNS が深刻なサイバー攻撃を受けた。"
    assert broken_translation_reason(_SRC, out) == "原文がそのまま写っている"


def test_proper_translation_passes() -> None:
    out = "南アフリカの Air Traffic and Navigation Services (ATNS) が深刻なサイバー攻撃を受けた。"
    assert broken_translation_reason(_SRC, out) is None


async def test_broken_output_raises_instead_of_caching() -> None:
    llm = FakeLLM(responses=['{"a": "b"}'])
    with pytest.raises(LLMError):
        await translate_body(llm, _SRC)


# ---------- 決定論関門 (translation_fidelity、2026-10-12: 検出のみ・保存しない) ----------

_GARBLED_SRC = "Gitea fixed a critical vulnerability."
_GARBLED_OUT = "Gite0 は重大な脆弱性を修正しました。"


async def test_gate_on_rejects_garbled_proper_noun_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRANSLATE_FIDELITY_GATE", "on")
    llm = FakeLLM(responses=[_GARBLED_OUT])
    with pytest.raises(TranslationGarbledError):
        await translate_body(llm, _GARBLED_SRC)
    assert len(llm.prompts) == 1  # 同じ位置で化けるので再試行しない


async def test_gate_default_is_shadow(monkeypatch: pytest.MonkeyPatch) -> None:
    """既定は shadow — 化けを検出しても訳は保存される (本番の発火を見てから on にする)。"""
    monkeypatch.delenv("TRANSLATE_FIDELITY_GATE", raising=False)
    llm = FakeLLM(responses=[_GARBLED_OUT])
    assert await translate_body(llm, _GARBLED_SRC)


async def test_gate_on_rejects_garbled_cve_and_number(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSLATE_FIDELITY_GATE", "on")
    with pytest.raises(TranslationGarbledError):
        await translate_body(
            FakeLLM(responses=["この欠陥は CVE-2016-76746 として追跡されています。"]),
            "The flaw is tracked as CVE-2026-76746.",
        )
    with pytest.raises(TranslationGarbledError):
        await translate_body(
            FakeLLM(responses=["このグループは 130 人を制裁しました。"]),
            "The group sanctioned 136 individuals.",
        )


async def test_gate_on_passes_faithful_translation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSLATE_FIDELITY_GATE", "on")
    out = await translate_body(
        FakeLLM(responses=["Gitea は CVE-2026-76746 を修正し、136 件に影響しました。"]),
        "Gitea fixed CVE-2026-76746 affecting 136 deployments.",
    )
    assert out == "Gitea は CVE-2026-76746 を修正し、136 件に影響しました。"


async def test_gate_shadow_keeps_text_and_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSLATE_FIDELITY_GATE", "shadow")
    with structlog.testing.capture_logs() as logs:
        out = await translate_body(FakeLLM(responses=[_GARBLED_OUT]), _GARBLED_SRC)
    assert out == _GARBLED_OUT  # 修復も拒否もしない
    events = [e for e in logs if e["event"] == "translate_fidelity_garbled"]
    assert len(events) == 1 and events[0]["mode"] == "shadow"
    assert _GARBLED_OUT not in str(events[0])  # 本文は log に出さない


async def test_gate_off_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSLATE_FIDELITY_GATE", "off")
    out = await translate_body(FakeLLM(responses=[_GARBLED_OUT]), _GARBLED_SRC)
    assert out == _GARBLED_OUT


def test_gate_mode_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    for raw, expected in [
        ("on", "on"),
        ("ident", "ident"),
        ("shadow", "shadow"),
        ("off", "off"),
        ("0", "off"),
        ("1", "on"),
        ("bogus", "shadow"),
        ("", "shadow"),
    ]:
        monkeypatch.setenv("TRANSLATE_FIDELITY_GATE", raw)
        assert fidelity_gate_mode() == expected


async def test_resumable_garbled_raises_and_keeps_no_garbled_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRANSLATE_FIDELITY_GATE", "on")
    store = MemoryChunkStore()
    with pytest.raises(TranslationGarbledError):
        await translate_body_resumable(
            FakeLLM(responses=[_GARBLED_OUT]), _GARBLED_SRC, article_id="a", store=store
        )
    assert store.rows == {}  # 化けた訳のチャンクは保存されない


# ---------- ident モード (識別子の化けだけで訳を保存しない) ----------

_CVE_SRC = "The flaw is tracked as CVE-2026-76746."
_CVE_OUT = "この欠陥は CVE-2016-76746 として追跡されています。"


async def test_ident_mode_blocks_garbled_identifier_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRANSLATE_FIDELITY_GATE", "ident")
    llm = FakeLLM(responses=[_CVE_OUT])
    with structlog.testing.capture_logs() as logs, pytest.raises(TranslationGarbledError):
        await translate_body(llm, _CVE_SRC)
    assert len(llm.prompts) == 1
    ev = [e for e in logs if e["event"] == "translate_fidelity_garbled"]
    assert len(ev) == 1
    assert ev[0]["blocked"] is True and ev[0]["identifiers"] == 1
    assert ev[0]["words"] == 0
    assert _CVE_OUT not in str(ev[0])


async def test_ident_mode_keeps_word_and_number_garble_but_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRANSLATE_FIDELITY_GATE", "ident")
    with structlog.testing.capture_logs() as logs:
        out_word = await translate_body(FakeLLM(responses=[_GARBLED_OUT]), _GARBLED_SRC)
        out_num = await translate_body(
            FakeLLM(responses=["このグループは 130 人を制裁しました。"]),
            "The group sanctioned 136 individuals.",
        )
    assert out_word == _GARBLED_OUT
    assert out_num.startswith("このグループ")
    ev = [e for e in logs if e["event"] == "translate_fidelity_garbled"]
    assert len(ev) == 2 and all(e["blocked"] is False for e in ev)
    assert ev[0]["words"] >= 1 and ev[1]["numbers"] >= 1


async def test_shadow_and_on_log_blocked_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSLATE_FIDELITY_GATE", "shadow")
    with structlog.testing.capture_logs() as logs:
        await translate_body(FakeLLM(responses=[_CVE_OUT]), _CVE_SRC)
    assert logs[0]["blocked"] is False


async def test_ident_mode_resumable_saves_no_garbled_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRANSLATE_FIDELITY_GATE", "ident")
    store = MemoryChunkStore()
    with pytest.raises(TranslationGarbledError):
        await translate_body_resumable(
            FakeLLM(responses=[_CVE_OUT]), _CVE_SRC, article_id="a", store=store
        )
    assert store.rows == {}
