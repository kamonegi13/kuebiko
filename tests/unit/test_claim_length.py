"""台帳 claim の長さの関門 (2026-11-20、11-19 監査 D1 P0-2b)。

改訂のたびに新しい被害組織を書き足し、1 件の claim が 64 字から 4,742 字まで伸びて、
状況総括の SIR 節を 6,000 字の途中で切らせていた (80 字以上伸びた改訂 170 件のうち 71 件が
純粋な追記)。上限 (400 字) 超・追記で 80 字超伸びる改訂は前回を保つ (旗 1)。
表示は長い claim を第 1 文にする。
"""

from __future__ import annotations

import pytest

from src.assessment.claim_length import (
    CLAIM_MAX_CHARS,
    check_claim_length,
    display_claim,
)
from src.tools.text_budget import cut_at_sentence

PRIOR = "X 社のメール配信サービスへの侵入で、複数の顧客企業から情報が流出した。"


def test_normal_revision_passes() -> None:
    assert (
        check_claim_length(PRIOR, "X 社のサービスへの侵入で、少なくとも 5 社の情報が流出した。")
        is None
    )


def test_too_long_revision_is_flagged() -> None:
    assert check_claim_length(PRIOR, "あ" * (CLAIM_MAX_CHARS + 1)) == "too_long"


def test_append_growth_is_flagged() -> None:
    revised = PRIOR + "さらに A 社・B 社・C 社・D 社・E 社・F 社・G 社・H 社が被害を公表し、" * 3
    assert check_claim_length(PRIOR, revised) == "append_growth"


@pytest.mark.parametrize("mode,expected", [("1", PRIOR), ("shadow", None), ("0", None)])
def test_gate_mode(monkeypatch: pytest.MonkeyPatch, mode: str, expected: str | None) -> None:
    from src.assessment.claim_length import apply_claim_length_gate

    monkeypatch.setenv("LEDGER_CLAIM_LENGTH_GATE", mode)
    revised = "あ" * (CLAIM_MAX_CHARS + 1)
    got = apply_claim_length_gate(PRIOR, revised)
    assert got == (expected or revised)


def test_display_shortens_long_claim_to_first_sentence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLOT_CLAIM_DISPLAY_FIRST_SENTENCE", "1")
    long_claim = "最初の文である。" + "続く文が長く続く。" * 60
    shown = display_claim(long_claim)
    assert len(shown) < 200 and shown.startswith("最初の文である")


def test_display_keeps_short_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLOT_CLAIM_DISPLAY_FIRST_SENTENCE", "1")
    assert display_claim(PRIOR) == PRIOR


def test_cut_at_sentence_never_cuts_mid_sentence() -> None:
    text = "一文目。二文目はとても長い文である。三文目。"
    assert cut_at_sentence(text, 12) == "一文目。"
