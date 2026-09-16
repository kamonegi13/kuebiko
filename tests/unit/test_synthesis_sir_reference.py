"""SIR ロールアップの番号参照化 (2026-09-17) — 既出の判定は id だけ、未掲載は claim を残す。"""

from __future__ import annotations

import pytest

from src.synthesis.grounded.estimate import KeyJudgment
from src.synthesis.grounded.render import _pir_rollup, sir_reference_mode


def _j(jid: str, claim: str, *, pir: str = "pir_x", implication: str = "含意") -> KeyJudgment:
    return KeyJudgment(
        id=jid,
        claim=claim,
        domain="cyber",
        leading_hypothesis="h",
        confidence="high",
        confidence_basis="b",
        hypotheses=(),
        evidence=(),
        pir_ids=(pir,),
        implication=implication,
    )


def test_reference_mode_keeps_claim_only_for_unshown_judgments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SYNTHESIS_SIR_REF", raising=False)
    js = (_j("s-aaa", "本文に載る判定"), _j("s-bbb", "本文に載らない判定"))

    out = _pir_rollup(js, shown_ids=frozenset({"s-aaa"}))

    assert sir_reference_mode() is True
    entries = out[0]["entries"]
    assert entries[0] == {"id": "s-aaa", "claim": "", "confidence_ja": "高確度", "implication": ""}
    assert entries[1]["id"] == "s-bbb" and entries[1]["claim"] == "本文に載らない判定"
    assert all(e["implication"] == "" for e in entries)


def test_flag_off_reproduces_legacy_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNTHESIS_SIR_REF", "0")
    js = (_j("s-aaa", "判定 A"),)

    out = _pir_rollup(js, shown_ids=frozenset({"s-aaa"}))

    assert sir_reference_mode() is False
    assert out[0]["entries"][0] == {
        "id": "",
        "claim": "判定 A",
        "confidence_ja": "高確度",
        "implication": "含意",
    }
