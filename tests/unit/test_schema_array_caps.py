"""配列の上限を schema で宣言し、文法に強制させる (2026-09-20)。

⚠ 暴走の実例: 事象ニュースの 1 窓で facts 84 件 (うち 64 件が重複)、spotlight の 1 窓で
key_events 126 件 (仕様 5-8)。**上限が宣言されていなければ、構造化出力の文法は
いくらでも続けることを許す**。要素ごとに「続ける / 閉じる」の賭けを繰り返すので、
確率がわずかに偏るだけで、いつか長い連続が出る (自己強化: Fu ら 2021、DITTO)。

実機で確認済み: Ollama は maxItems を文法へコンパイルし、**モデルが続けたくても閉じさせる**
(「1 から 20 を入れよ」に対し maxItems=3 なら 3 件で停止)。

⚠⚠ **上限は暴走を止めるが重複は止めない** (maxItems=8 でも同じ行を 8 個出せる)。
uniqueItems は文脈自由文法で表現できないので原理的に不可。重複は別途 seam で落とす。

上限値は**重複のない窓の実測**から置く (155 窓):
facts 中央 8 / 99% 34 / 最大 49 → 60。相違 99% 4 → 12。unknowns 99% 9 → 20。
"""

from __future__ import annotations

import pytest

from src.eventnews.models import EventNewsDraft
from src.spotlight.generator import _LLMSpotlightOutput

_EVENT_CAPS = {
    "key_points": 12,
    "facts": 60,
    "discrepancies": 12,
    "caveats": 12,
    "unknowns": 20,
}
_SPOTLIGHT_CAPS = {"key_events": 15, "caveats": 12, "unknowns": 20}


@pytest.mark.parametrize(("field", "cap"), sorted(_EVENT_CAPS.items()))
def test_event_news_arrays_declare_a_cap(field: str, cap: int) -> None:
    """文法が閉じを強制できるよう、全配列に上限を宣言する。"""
    prop = EventNewsDraft.model_json_schema()["properties"][field]

    assert prop.get("maxItems") == cap, f"{field} に maxItems が無い (暴走を止められない)"


@pytest.mark.parametrize(("field", "cap"), sorted(_SPOTLIGHT_CAPS.items()))
def test_spotlight_arrays_declare_a_cap(field: str, cap: int) -> None:
    prop = _LLMSpotlightOutput.model_json_schema()["properties"][field]

    assert prop.get("maxItems") == cap


def test_caps_leave_headroom_over_the_observed_legitimate_maximum() -> None:
    """⚠ 上限は**正当な出力を切らない**こと。重複のない 155 窓で facts は最大 49 件。"""
    assert _EVENT_CAPS["facts"] >= 49 * 1.2
    # 相違と unknowns は 99 パーセンタイルが 4 件と 9 件。暴走 (44, 192) とは桁が違う
    assert _EVENT_CAPS["discrepancies"] >= 4 * 2
    assert _EVENT_CAPS["unknowns"] >= 9 * 2


# ---------- 上限は「文法への宣言」であって「入力の拒否」ではない ----------


def test_over_cap_input_is_accepted_not_rejected() -> None:
    """⚠⚠ **上限超過で例外にしてはいけない**。

    文法が上限を守るのは**生成時だけ**。JSON 修復経路・外部 LLM・保存済みデータからは
    超過が届きうる。pydantic の ``max_length`` は検証なので、そのままだと本番が
    ValidationError で落ちる (2026-09-20 に既存テスト 2 件がこれで落ちて気付いた)。
    ``json_schema_extra`` で **schema にだけ**上限を出す。
    """
    draft = EventNewsDraft(headline="h", bluf="b", unknowns=["x"] * 30)

    assert len(draft.unknowns) == 30  # 受け取りは拒否も切り捨てもしない


def test_spotlight_over_cap_input_is_accepted() -> None:
    out = _LLMSpotlightOutput(headline="h", key_events=[], unknowns=["x"] * 50)

    assert len(out.unknowns) == 50


def test_existing_dedup_collapses_loops_without_losing_later_items() -> None:
    """⚠ **切り捨てを model でやってはいけない** — `dedup_draft` が畳む前に切ると、
    後ろの正当な要素が消える (「u1 × 300 + u2」が「u1」だけになった)。
    重複の除去は既存の `list_dedup` が担い、model は素通しにする。
    """
    from src.eventnews.list_dedup import dedup_draft

    draft = EventNewsDraft(headline="h", bluf="b", unknowns=["u1"] * 300 + ["u2"])
    out, removed = dedup_draft(draft)

    assert out.unknowns == ["u1", "u2"]
    assert removed["unknowns"] == 299
