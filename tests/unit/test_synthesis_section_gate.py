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


def test_section_cap_is_applied_after_generation_not_in_the_grammar() -> None:
    """節の上限は **生成後に決定論で切る** (2026-09-26 に反転)。

    2026-09-21 に maxLength を文法へ載せたところ暴走の引き金になった (同じ入力で上限あり 2/2 暴走・
    なし 0/2)。上限値は実データから — weekly の正常な最大 3,554 字を切らない。
    """
    from src.synthesis.grounded.render import (
        _HEADLINE_MAX_CHARS,
        _SECTION_MAX_CHARS,
        _collapse_runaway,
    )

    props = _WireSections.model_json_schema()["properties"]
    assert all("maxLength" not in props[c] for c in props)
    assert _SECTION_MAX_CHARS >= 3_554 and _HEADLINE_MAX_CHARS < _SECTION_MAX_CHARS
    # 繰り返しのない正常な文で 3,500 字前後 (= 正常な最大付近)
    normal = "".join(f"事象{i}の見立ては確度中程度である。" for i in range(200))
    assert (
        _collapse_runaway(_WireSections(weight_section=normal), period_label="t").weight_section
        == normal
    )
