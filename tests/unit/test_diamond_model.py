"""Diamond Model 2 軸モジュールの unit tests (Phase Diamond-Axes)。"""

from __future__ import annotations

import pytest

from src.cti.diamond_model import (
    SOCIO_POLITICAL_INTENTS,
    DiamondAxes,
    SocioPoliticalAxis,
    intent_label_ja,
    intent_to_stix_motivation,
    normalize_intent,
    parse_diamond_axes,
)

# 制御文字 (backslash escape を source に書かず chr() で組む)
NEWLINE = chr(10)
TAB = chr(9)
BELL = chr(7)

# ---------- normalize_intent ----------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("espionage", "espionage"),
        ("ESPIONAGE", "espionage"),
        ("  Espionage  ", "espionage"),
        ("financial", "financial"),
        ("prepositioning", "prepositioning"),
        ("pre-positioning", "prepositioning"),  # alias
        ("pre_positioning", "prepositioning"),  # underscore → hyphen
        ("disruption", "disruption"),
        ("influence", "influence"),
        ("hacktivism", "hacktivism"),
        # 地政学/国家 動機 (Phase Geopolitical-Intent)
        ("coercion", "coercion"),
        ("deterrence", "deterrence"),
        ("territorial", "territorial"),
        ("subversion", "subversion"),
        ("diplomacy", "diplomacy"),
        ("unknown", "unknown"),
    ],
)
def test_normalize_intent_canonical_and_case(raw: str, expected: str) -> None:
    assert normalize_intent(raw) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("cyber-espionage", "espionage"),
        ("data-theft", "espionage"),
        ("ransomware", "financial"),
        ("extortion", "financial"),
        ("sabotage", "disruption"),
        ("wiper", "disruption"),
        ("ddos", "disruption"),
        ("disinformation", "influence"),
        ("infoops", "influence"),
        ("hacktivist", "hacktivism"),
        ("foothold", "prepositioning"),
        ("諜報", "espionage"),
        ("金銭目的", "financial"),
        # 地政学/国家 動機の alias
        ("sanctions", "coercion"),
        ("compellence", "coercion"),
        ("威圧", "coercion"),
        ("deter", "deterrence"),
        ("抑止", "deterrence"),
        ("sovereignty", "territorial"),
        ("annexation", "territorial"),
        ("領土", "territorial"),
        ("regime-change", "subversion"),
        ("destabilization", "subversion"),
        ("alliance", "diplomacy"),
        ("treaty", "diplomacy"),
        ("外交", "diplomacy"),
    ],
)
def test_normalize_intent_alias_absorption(raw: str, expected: str) -> None:
    assert normalize_intent(raw) == expected


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["", "  ", "banana", "lateral-movement", None, 42, [], {}])
def test_normalize_intent_unknown_fallback(raw: object) -> None:
    # 未知語・非 str はすべて unknown に倒れる
    assert normalize_intent(raw) == "unknown"


@pytest.mark.unit
def test_normalize_intent_result_always_canonical() -> None:
    # 出力は必ず canonical 集合の要素
    for raw in ["espionage", "garbage", "ransomware", None]:
        assert normalize_intent(raw) in SOCIO_POLITICAL_INTENTS


# ---------- intent_label_ja ----------


@pytest.mark.unit
def test_intent_label_ja_known_and_unknown() -> None:
    assert intent_label_ja("espionage") == "諜報・情報窃取"
    assert intent_label_ja("financial") == "金銭目的"
    # 未知 canonical でも「不明」に倒れて KeyError にしない
    assert intent_label_ja("nonexistent") == "不明"


# ---------- intent_to_stix_motivation ----------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("intent", "expected"),
    [
        ("espionage", "organizational-gain"),
        ("financial", "personal-gain"),
        ("prepositioning", "dominance"),
        ("disruption", "coercion"),
        ("influence", "ideology"),
        ("hacktivism", "ideology"),
        # 地政学動機: 近似 or None (STIX は cyber-attacker 動機語彙のみ)
        ("coercion", "coercion"),
        ("territorial", "dominance"),
        ("subversion", "dominance"),
        ("deterrence", None),  # 防御的 → STIX に該当なし
        ("diplomacy", None),  # 協調的 → STIX に該当なし
        ("unknown", None),
        ("garbage", None),  # 未知は None
    ],
)
def test_intent_to_stix_motivation(intent: str, expected: str | None) -> None:
    assert intent_to_stix_motivation(intent) == expected


# ---------- parse_diamond_axes ----------


@pytest.mark.unit
def test_parse_diamond_axes_full_object() -> None:
    # Arrange
    raw = {
        "socio_political": {
            "intent": "espionage",
            "rationale": "国家系 APT が防衛産業から機密窃取",
            "confidence": "high",
        },
        "technical": "Cobalt Strike beacon を Cloudflare fronted C2 経由で運用",
    }
    # Act
    axes = parse_diamond_axes(raw)
    # Assert
    assert axes.socio_political.intent == "espionage"
    assert axes.socio_political.rationale == "国家系 APT が防衛産業から機密窃取"
    assert axes.socio_political.confidence == "high"
    assert "Cobalt Strike" in axes.technical
    assert axes.has_signal is True


@pytest.mark.unit
def test_parse_diamond_axes_missing_returns_safe_default() -> None:
    # 欠落 / 非 dict はすべて安全側デフォルトへ
    raws: list[object] = [None, "", 123, [], "espionage"]
    for raw in raws:
        axes = parse_diamond_axes(raw)
        assert axes.socio_political.intent == "unknown"
        assert axes.technical == ""
        assert axes.has_signal is False


@pytest.mark.unit
def test_parse_diamond_axes_socio_political_as_bare_string() -> None:
    # socio_political を object でなく string で返す LLM への耐性
    axes = parse_diamond_axes({"socio_political": "ransomware"})
    assert axes.socio_political.intent == "financial"


@pytest.mark.unit
def test_parse_diamond_axes_garbage_intent_falls_back() -> None:
    axes = parse_diamond_axes({"socio_political": {"intent": "banana", "confidence": "weird"}})
    assert axes.socio_political.intent == "unknown"
    assert axes.socio_political.confidence == "low"  # 不正 confidence は low


@pytest.mark.unit
def test_parse_diamond_axes_truncates_and_sanitizes_oneliners() -> None:
    # 長文・改行・タブ・制御文字を含む rationale / technical を整形
    dirty_technical = "x" + NEWLINE + TAB + "y  multi   space" + BELL + "ctrl" + ("z" * 200)
    raw = {
        "socio_political": {
            "intent": "financial",
            "rationale": "あ" * 200,  # 80 字上限
        },
        "technical": dirty_technical,
    }
    axes = parse_diamond_axes(raw)
    assert len(axes.socio_political.rationale) <= 80
    assert len(axes.technical) <= 120
    assert NEWLINE not in axes.technical
    assert TAB not in axes.technical
    assert BELL not in axes.technical  # 制御文字は除去
    assert "multi space" in axes.technical  # 連続空白の畳み込み
    assert "ctrl" in axes.technical  # 可視文字は保持


@pytest.mark.unit
def test_diamond_axes_default_construction() -> None:
    # 引数なし生成は安全側デフォルト
    axes = DiamondAxes()
    assert isinstance(axes.socio_political, SocioPoliticalAxis)
    assert axes.socio_political.intent == "unknown"
    assert axes.technical == ""
    assert axes.has_signal is False


@pytest.mark.unit
def test_socio_political_axis_is_frozen() -> None:
    axis = SocioPoliticalAxis(intent="espionage")
    with pytest.raises(Exception):  # noqa: B017,PT011 (frozen 違反は ValidationError)
        axis.intent = "financial"
