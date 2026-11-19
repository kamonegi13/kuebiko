"""翻訳の JSON の形の判定から、原文の JSON をそのまま残した訳を外す (2026-11-19)。"""

from __future__ import annotations

import pytest

from src.cti.body_translator import broken_translation_reason
from src.cti.translate_json_shape import novel_json_shapes

_JSON_REASON = "JSON の形が混ざっている"

_SRC = (
    "The implant beacons to its controller with the following configuration:\n\n"
    '{"server": "203.0.113.5", "interval": "60"}\n\n'
    "Operators rotate the server value weekly."
)
#: 原文の JSON を空白だけ変えて残した正しい訳
_RETAINED = (
    "このインプラントは次の設定でコントローラへビーコンを送る。\n\n"
    '{ "server":"203.0.113.5", "interval": "60" }\n\n'
    "運用者は server の値を毎週入れ替えている。"
)
#: 10-02 型の崩れ: 原文の文を鍵にした対応表
_COLLAPSED = (
    '{"Operators rotate the server value weekly.": "運用者は server の値を毎週入れ替えている。"}'
)


def test_counts_only_shapes_missing_from_source() -> None:
    assert novel_json_shapes(_SRC, _RETAINED) == 0
    assert novel_json_shapes(_SRC, _COLLAPSED) == 1


def test_default_off_keeps_legacy_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRANSLATE_JSON_SOURCE_EXEMPT", raising=False)
    assert broken_translation_reason(_SRC, _RETAINED) == _JSON_REASON


def test_shadow_does_not_change_the_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSLATE_JSON_SOURCE_EXEMPT", "shadow")
    assert broken_translation_reason(_SRC, _RETAINED) == _JSON_REASON


def test_on_exempts_json_retained_from_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSLATE_JSON_SOURCE_EXEMPT", "1")
    assert broken_translation_reason(_SRC, _RETAINED) is None


def test_on_still_blocks_mapping_collapse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSLATE_JSON_SOURCE_EXEMPT", "1")
    assert broken_translation_reason(_SRC, _COLLAPSED) == _JSON_REASON
    # 原文の JSON を残しつつ対応表も混ざれば、やはり壊れている
    assert broken_translation_reason(_SRC, _RETAINED + "\n" + _COLLAPSED) == _JSON_REASON


def test_on_blocks_collapse_when_source_has_no_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSLATE_JSON_SOURCE_EXEMPT", "1")
    src = "Start your day with intelligence."
    out = '{"Start your day with intelligence.": "一日をインテリジェンスとともに。"}'
    assert broken_translation_reason(src, out) == _JSON_REASON
