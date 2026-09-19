"""地政学の文脈情勢の予約枠 (2026-09-19)。

サイバー = 追跡の主力 / 地政学 = 判断を支える文脈。**除外ではなく配分** — サイバーは物理的な
地政学に影響され、また与えるため (利用者指摘)。地政学は証拠量ではなく**鮮度**で選ぶ。
"""

from __future__ import annotations

import pytest

from src.assessment.stateful import geo_reserve, split_geo_reserved

_CAND = {
    "cyber-a": ["x1", "x2", "x3"],
    "cyber-b": ["y1"],
    "geo-old": ["g1"],
    "geo-new": ["g2", "g3", "g4", "g5"],
    "geo-mid": ["g6"],
}
_DOMAIN = {
    "cyber-a": "cyber_incident",
    "cyber-b": "infrastructure",
    "geo-old": "geopolitical",
    "geo-new": "military",
    "geo-mid": "political",
}
_REV_AT = {
    "cyber-a": "2026-09-19",
    "geo-old": "2026-09-01",
    "geo-mid": "2026-09-10",
    "geo-new": "2026-09-18",
}


def test_geo_is_picked_by_staleness_not_evidence_volume() -> None:
    rest, reserved = split_geo_reserved(_CAND, _DOMAIN, _REV_AT, reserve=2)

    # 証拠 4 件の geo-new ではなく、判定が古い geo-old / geo-mid が予約される
    assert set(reserved) == {"geo-old", "geo-mid"}
    assert set(rest) == {"cyber-a", "cyber-b"}
    # 予約に漏れた geo は rest にも入らない (通常の競争に混ぜない = 元の偏りに戻さない)
    assert "geo-new" not in rest


def test_reserve_zero_keeps_legacy_behaviour() -> None:
    rest, reserved = split_geo_reserved(_CAND, _DOMAIN, _REV_AT, reserve=0)
    assert reserved == {} and rest == _CAND


def test_unknown_domain_is_treated_as_cyber_side() -> None:
    rest, reserved = split_geo_reserved({"x": ["a"]}, {"x": ""}, {}, reserve=2)
    assert set(rest) == {"x"} and reserved == {}


def test_reserve_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SYNTHESIS_GEO_RESERVE", raising=False)
    assert geo_reserve() == 2
    monkeypatch.setenv("SYNTHESIS_GEO_RESERVE", "0")
    assert geo_reserve() == 0
    monkeypatch.setenv("SYNTHESIS_GEO_RESERVE", "4")
    assert geo_reserve() == 4
