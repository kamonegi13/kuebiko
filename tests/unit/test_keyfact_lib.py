"""要点シート評価の部品 (scripts/keyfact_lib.py、2026-09-26)。"""

from __future__ import annotations

import json

from scripts.keyfact_lib import (
    Judgment,
    KeyFact,
    mcnemar_p,
    render_draft,
    settle,
    source_block,
    verified_facts,
)


def _fact(quote: str) -> KeyFact:
    return KeyFact(
        category="被害",
        slot="誰の",
        fact="x",
        certainty="主張・報道",
        importance="核心",
        source_index=1,
        quote=quote,
    )


def test_source_block_cuts_between_articles_and_catalog() -> None:
    prompt = "前置き\n## 対象記事\n[1] 本文\n## 識別子カタログ\n後ろ"
    assert source_block(prompt) == "## 対象記事\n[1] 本文\n"
    assert source_block("記事見出しなし") == "記事見出しなし"


def test_facts_without_real_quote_are_dropped() -> None:
    sources = "Four victims so far: RapidFort, Dynatrace"
    kept, dropped = verified_facts(
        [_fact("Four  victims so far"), _fact("Five victims"), _fact("")], sources
    )
    assert len(kept) == 1 and dropped == 2  # 空白の違いは許す・作り話と空は捨てる


def test_render_draft_lists_reader_visible_fields() -> None:
    raw = json.dumps(
        {
            "headline": "H",
            "bluf": "B",
            "key_points": ["K"],
            "unknowns": ["U"],
            "facts": [{"text": "F"}],
            "discrepancies": [],
            "caveats": [{"text": "C"}],
        }
    )
    text = render_draft(raw)
    for part in ("見出し: H", "BLUF: B", "要点: K", "不明点: U", "事実: F", "但し書き: C"):
        assert part in text
    assert render_draft(None) == ""


def test_settle_requires_real_quote_and_fills_missing_ids() -> None:
    summary = "見出し: 4 組織が掲載されたと主張されている"
    judged = [
        Judgment(id=1, present=True, distortion="なし", quote="4 組織が掲載された"),
        Judgment(id=2, present=True, distortion="確度の格上げ", quote="存在しない引用"),
        Judgment(id=9, present=True, distortion="なし", quote="4 組織"),  # 範囲外は無視
    ]
    out = settle(judged, 3, summary)
    assert [r["present"] for r in out] == [True, False, False]
    assert out[1]["flag"] == "unverified" and out[1]["distortion"] == "なし"
    assert out[2]["flag"] == "judge_missing"


def test_mcnemar_is_symmetric_and_neutral_without_discordance() -> None:
    assert mcnemar_p(0, 0) == 1.0
    assert mcnemar_p(10, 0) == mcnemar_p(0, 10) < 0.01
