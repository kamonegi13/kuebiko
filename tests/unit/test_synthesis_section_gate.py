"""状況総括の節の欠落と長さの関門 (2026-09-21)。

週次総括が narrative の途中切れで壊れた。weight_section が 17,460 字に膨れ、
残り 4 節が空のまま DB へ入り、``synthesis_persisted`` が正常ログを出していた
(外形は成功・中身は欠落)。さらに巨大な本文が Discord に拒否され配信も失われた。
"""

from __future__ import annotations

from dataclasses import dataclass

from src.synthesis.grounded.render import _WireSections
from src.synthesis.runner import _empty_sections


@dataclass
class _Rec:
    headline: str = "見出し"
    weight_section: str = "比重"
    chain_section: str = "連鎖"
    cog_section: str = "重心"
    spillover_section: str = "波及"
    pir_section: str = "PIR"


def test_complete_record_has_no_empty_sections() -> None:
    assert _empty_sections(_Rec()) == []


def test_names_every_missing_section() -> None:
    """実際に起きた形 — 1 節だけ残り 4 節が空。"""
    rec = _Rec(chain_section="", cog_section="", spillover_section="", pir_section="")

    assert _empty_sections(rec) == [
        "chain_section",
        "cog_section",
        "spillover_section",
        "pir_section",
    ]


def test_whitespace_only_counts_as_empty() -> None:
    assert _empty_sections(_Rec(cog_section="   \n ")) == ["cog_section"]


def test_schema_caps_section_length_so_the_grammar_can_close() -> None:
    """配列が無いので maxItems は使えない。文字列でも暴走するため maxLength を載せる。

    上限は実データから決める — weekly の正常な最大は 3,554 字なので、6,000 なら
    正常な出力を一切切らない。
    """
    props = _WireSections.model_json_schema()["properties"]

    assert props["weight_section"]["maxLength"] >= 3_554  # 実測の正常最大を切らない
    assert all(props[c].get("maxLength") for c in ("chain_section", "cog_section", "pir_section"))
    assert props["headline"]["maxLength"] < props["weight_section"]["maxLength"]
