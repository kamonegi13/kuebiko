"""対読審判の採点者を差し替えられるようにする (2026-09-19)。

審判 (Sonnet) が「n17c は接地が悪い」と 13 窓中 12 窓で判定した一方、独立した接地検証器
(s17) は腕の差を再現しなかった。**三者目が要る**。そのとき採点者がどちらとも違うモデルで
あることは欠点ではなく独立性の利点になる。

⚠ 出力の生成と違い、採点は本番の経路を再現する必要がない。両腕に同じ物差しを当てれば
比較は成立する。ただし**使う前に検出力を確かめる** (負のコントロール)。
"""

from __future__ import annotations

import pytest

from scripts.judge_eventnews_pairwise import build_judge_client


def test_default_is_the_external_bridge() -> None:
    """既定は従来どおり claude-code bridge (過去の審判と同じ土俵)。"""
    c = build_judge_client(provider="claude-code", model="sonnet", base_url="http://127.0.0.1:8010")

    assert type(c).__name__ == "ClaudeCodeClient"


def test_ollama_provider_targets_the_given_host() -> None:
    """別マシンの Ollama を採点者にできる (base_url で指定)。"""
    c = build_judge_client(
        provider="ollama", model="gemma3:12b", base_url="http://192.168.1.100:11434"
    )

    assert type(c).__name__ == "OllamaClient"
    assert c.model == "gemma3:12b"


def test_forbidden_models_are_refused_for_the_judge_too() -> None:
    """中華系の denylist は採点者にも効く (CLAUDE.md §4 はプロバイダ横断)。"""
    from src.tools.llm_client import LLMForbiddenModelError

    with pytest.raises(LLMForbiddenModelError):
        build_judge_client(
            provider="ollama", model="qwen3:14b", base_url="http://192.168.1.100:11434"
        )


def test_unknown_provider_fails_loudly() -> None:
    with pytest.raises(ValueError, match="provider"):
        build_judge_client(provider="mystery", model="x", base_url="http://127.0.0.1:1")
