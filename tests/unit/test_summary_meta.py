"""要約に重要度の理由づけ (メタ文) が混入するのを除く関門 (2026-11-20、11-19 監査 D4 c)。

「…ため high とはしない」「重要度は medium と判断」が利用者向けの要約に約 1%/日出ていた
(s24ci65 の時期から、s26r 固有ではない)。重要度の判断は欄 (importance) が持つ。
CVSS や引用 (「high」と評価) の形には当てない。
"""

from __future__ import annotations

import pytest

from src.pipeline.summary_meta import find_meta_sentences, strip_meta_sentences

BODY = "攻撃者は CVE-2026-1234 を悪用し、複数の組織に侵入した。修正版が公開されている。"


@pytest.mark.parametrize(
    "sentence",
    [
        "悪用の報告は無いため high とはしない。",
        "重要度は medium と判断した。",
        "日本への影響は限定的なので low とする。",
    ],
)
def test_finds_meta_sentence(sentence: str) -> None:
    assert find_meta_sentences(BODY + sentence) == [sentence]


@pytest.mark.parametrize(
    "text",
    [
        "CVSS は 9.8 (Critical) で、深刻度は high に分類される。",
        "ベンダーは影響を「high」と評価している。",
        "高 (high) 62 件、中 (medium) 30 件を修正した。",
        "うち 6 件が Critical、5 件が High と評価された。",
    ],
)
def test_ignores_quotes_and_scores(text: str) -> None:
    assert find_meta_sentences(text) == []


def test_strip_removes_only_meta_sentence() -> None:
    text = BODY + "悪用の報告は無いため high とはしない。"
    assert strip_meta_sentences(text) == BODY


def test_strip_keeps_text_when_too_little_remains() -> None:
    text = "短い。重要度は low とする。"
    assert strip_meta_sentences(text) == text


def test_briefing_guard_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from src.pipeline import briefing

    class _S(SimpleNamespace):
        def model_copy(self, update: dict[str, str]) -> _S:
            return _S(**{**self.__dict__, **update})

    meta = BODY + "悪用の報告は無いため high とはしない。"
    s = _S(summary=meta, bluf="")
    monkeypatch.setenv("SUMMARY_META_GUARD", "shadow")
    assert briefing._guard_meta_sentences(s, article_id="a").summary == meta  # type: ignore[arg-type]
    monkeypatch.setenv("SUMMARY_META_GUARD", "1")
    assert briefing._guard_meta_sentences(s, article_id="a").summary == BODY  # type: ignore[arg-type]
