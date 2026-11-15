"""翻訳の保持行・URL の素通し (src/cti/translate_passthrough.py + body_translator の配線)。"""

from __future__ import annotations

import hashlib

import pytest

from src.cti.body_translator import BODY_TRANSLATE_SYSTEM_PROMPT, translate_body
from src.cti.translate_passthrough import (
    PROSE_MIN_LETTERS,
    is_retainable_line,
    passthrough_mode,
    protect_chunk,
    restore_chunk,
)
from tests.unit.test_body_translator import FakeLLM

PROSE = "The attackers abused a misconfigured gateway to deploy a loader across the network. "
URL_A = "https://example.test/ioc/2998:2048/payload"
URL_B = "http://ichi_twnovel.example.test/x"
# 本番のプロンプトの固定 (従来経路が 1 バイトも変わらないことの固定)
_PLAIN_PROMPT_SHA = hashlib.sha256(BODY_TRANSLATE_SYSTEM_PROMPT.encode()).hexdigest()


def test_passthrough_mode_defaults_and_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRANSLATE_RETAIN_PASSTHROUGH", raising=False)
    assert passthrough_mode() == "0"
    monkeypatch.setenv("TRANSLATE_RETAIN_PASSTHROUGH", "shadow")
    assert passthrough_mode() == "shadow"
    monkeypatch.setenv("TRANSLATE_RETAIN_PASSTHROUGH", "1")
    assert passthrough_mode() == "1"
    monkeypatch.setenv("TRANSLATE_RETAIN_PASSTHROUGH", "bogus")
    assert passthrough_mode() == "0"


@pytest.mark.parametrize(
    "line",
    [
        URL_A,
        "- " + URL_B,
        "d41d8cd98f00b204e9800998ecf8427e",
        "10.0.0.1:8080",
        "evil.example.test",
        "![logo](https://example.test/a.png)",
    ],
)
def test_is_retainable_line_true(line: str) -> None:
    assert is_retainable_line(line)


@pytest.mark.parametrize(
    "line", ["Introduction", "The attackers used " + URL_A, "", "co-operation"]
)
def test_is_retainable_line_false(line: str) -> None:
    assert not is_retainable_line(line)


def test_protect_groups_runs_and_fences() -> None:
    chunk = f"{PROSE}\n\n{URL_A}\n{URL_B}\n\n```\ncmd /c x\n\nmore\n```\n\n{PROSE}"
    p = protect_chunk(chunk)
    assert len(p.runs) == 2
    assert p.runs[0] == f"{URL_A}\n{URL_B}"
    assert "⟦K1⟧" in p.text and "⟦K2⟧" in p.text and URL_A not in p.text


def test_inline_url_replaced_and_trailing_punct_kept() -> None:
    p = protect_chunk(f"See {URL_A}, and then it spread widely through the whole network today.")
    assert p.inline_urls == ((0, URL_A),)
    assert "⟦U1⟧," in p.text


def test_restore_reinserts_dropped_run_after_previous_token() -> None:
    chunk = f"{PROSE}\n\n{URL_A}\n\n{PROSE}\n\n{URL_B}"
    p = protect_chunk(chunk)
    translated = "訳1\n\n⟦K1⟧\n\n訳2"  # K2 が落ちた
    r = restore_chunk(p, translated)
    assert r.restored_runs == 1
    assert URL_A in r.text and URL_B in r.text
    assert r.text.index(URL_A) < r.text.index(URL_B)


def test_restore_puts_first_dropped_run_at_start() -> None:
    p = protect_chunk(f"{URL_A}\n\n{PROSE}")
    r = restore_chunk(p, "訳のみ")
    assert r.text.startswith(URL_A)


def test_restore_appends_missing_inline_url_to_its_paragraph() -> None:
    p = protect_chunk(f"{PROSE}\n\nDetails at {URL_A} are listed there for everyone to read.")
    r = restore_chunk(p, "訳1\n\n詳細は参照")
    assert r.missing_urls == 1
    assert r.text.split("\n\n")[1].endswith(URL_A)


def test_url_text_the_model_would_alter_never_reaches_it() -> None:
    p = protect_chunk(f"{PROSE}\n{URL_A}")
    assert URL_A not in p.text
    r = restore_chunk(p, "訳\n⟦K1⟧")
    assert URL_A in r.text


async def test_url_only_source_skips_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSLATE_RETAIN_PASSTHROUGH", "1")
    llm = FakeLLM()
    source = f"{URL_A}\n{URL_B}\nShort title"
    assert protect_chunk(source).prose_letters < PROSE_MIN_LETTERS
    assert await translate_body(llm, source) == source
    assert llm.prompts == []


async def test_mode1_restores_dropped_and_sends_no_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSLATE_RETAIN_PASSTHROUGH", "1")
    llm = FakeLLM(["攻撃者は設定不備のゲートウェイを悪用した。"])  # K1 を落とす
    source = f"{PROSE}{PROSE}\n\n{URL_A}"
    out = await translate_body(llm, source)
    assert URL_A in out and "攻撃者" in out
    assert URL_A not in llm.prompts[0]
    assert "⟦K1⟧" in llm.prompts[0]
    assert "⟦" in (llm.kwargs[0]["system"] or "")


async def test_mode1_not_echo_failure_on_reinserted_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSLATE_RETAIN_PASSTHROUGH", "1")
    monkeypatch.delenv("TRANSLATE_ECHO_EXEMPT_RETAINABLE", raising=False)
    long_url = "https://example.test/" + "a" * 120
    llm = FakeLLM(["攻撃者は設定不備を悪用した。\n⟦K1⟧"])
    out = await translate_body(llm, f"{PROSE}{PROSE}\n\n{long_url}")
    assert long_url in out


async def test_mode0_prompt_and_system_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRANSLATE_RETAIN_PASSTHROUGH", raising=False)
    llm = FakeLLM(["訳"])
    source = f"{PROSE}{PROSE}".strip() + f"\n\n{URL_A}"
    await translate_body(llm, source)
    assert llm.prompts[0] == f"次の記事本文を日本語に翻訳してください:\n\n{source}"
    assert llm.kwargs[0]["system"] == BODY_TRANSLATE_SYSTEM_PROMPT
    assert hashlib.sha256(llm.kwargs[0]["system"].encode()).hexdigest() == _PLAIN_PROMPT_SHA


async def test_shadow_keeps_old_output_and_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSLATE_RETAIN_PASSTHROUGH", "shadow")
    llm = FakeLLM(["訳のみ"])
    source = f"{URL_A}\n{URL_B}"
    out = await translate_body(llm, source)
    assert out == "訳のみ"
    assert llm.prompts == [f"次の記事本文を日本語に翻訳してください:\n\n{source}"]


def test_mangled_run_token_is_restored_without_leftover() -> None:
    """モデルが ⟦K1⟧ を [K1] / 【Ｋ１】 に崩しても、原文の行が 1 回だけ戻り崩れた記号は残らない。"""
    chunk = "Intro sentence here.\nhttps://example.com/a\nMore text here.\nhttps://example.com/b"
    protected = protect_chunk(chunk)
    result = restore_chunk(protected, "導入文。\n[K1]\nさらに本文。\n【Ｋ２】")
    assert result.text.count("https://example.com/a") == 1
    assert result.text.count("https://example.com/b") == 1
    assert "[K1]" not in result.text
    assert "Ｋ２" not in result.text
    assert result.restored_runs == 0


def test_bracketed_text_outside_token_range_is_left_alone() -> None:
    """番号が実在の範囲外の [K9] は原文の語として残す。"""
    protected = protect_chunk("Intro.\nhttps://example.com/a")
    result = restore_chunk(protected, "導入。[K9]\n⟦K1⟧")
    assert "[K9]" in result.text
