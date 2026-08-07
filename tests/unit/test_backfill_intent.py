"""intent backfill スクリプト (scripts/backfill_intent.py) の純粋ロジックの unit test。

DB / LLM に触れない部分 (cron guard 判定 / プロンプト組み立て / 出力正規化) を検証する。
正規化は ingest と同じ parse_diamond_axes を経由するのが要件 (レジーム一致)。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from backfill_intent import (  # noqa: E402
    BODY_MAX_CHARS,
    _IntentOut,
    build_prompt,
    guard_reason,
    normalize_result,
)


def _at(hour: int, minute: int) -> datetime:
    return datetime(2026, 7, 12, hour, minute, 0)


class TestGuardReason:
    def test_hourly_rss_window_starts_at_minute_57(self) -> None:
        assert guard_reason(_at(12, 57)) == "hourly-rss"

    def test_hourly_rss_window_ends_at_minute_16(self) -> None:
        assert guard_reason(_at(12, 16)) == "hourly-rss"

    def test_minute_17_is_clear(self) -> None:
        assert guard_reason(_at(12, 17)) is None

    def test_morning_brief_window(self) -> None:
        assert guard_reason(_at(6, 20)) == "morning-brief"
        assert guard_reason(_at(7, 45)) == "morning-brief"

    def test_evening_synthesis_window(self) -> None:
        assert guard_reason(_at(19, 30)) == "evening-synthesis"

    def test_normal_daytime_is_clear(self) -> None:
        assert guard_reason(_at(12, 30)) is None
        assert guard_reason(_at(8, 30)) is None


class TestBuildPrompt:
    def test_includes_title_and_category(self) -> None:
        prompt = build_prompt("APT41 が新 C2 展開", "apt", "本文" * 200, None)
        assert "APT41 が新 C2 展開" in prompt
        assert "カテゴリ: apt" in prompt

    def test_body_is_truncated(self) -> None:
        long_body = "x" * (BODY_MAX_CHARS + 5000)
        prompt = build_prompt("t", "apt", long_body, None)
        assert len(prompt) < BODY_MAX_CHARS + 3000  # テンプレート分を含めても上限近傍

    def test_short_body_falls_back_to_summary(self) -> None:
        prompt = build_prompt("t", "apt", "短い", "これは要約テキストです" * 10)
        assert "これは要約テキストです" in prompt

    def test_none_body_falls_back_to_summary(self) -> None:
        prompt = build_prompt("t", None, None, "要約のみ存在する")
        assert "要約のみ存在する" in prompt
        assert "カテゴリ: unknown" in prompt


class TestNormalizeResult:
    def test_alias_is_normalized_to_canonical(self) -> None:
        out = _IntentOut(intent="pre-positioning", confidence="low", rationale="足場確保の報告")
        intent, confidence, rationale = normalize_result(out)
        assert intent == "prepositioning"
        assert confidence == "low"
        assert rationale == "足場確保の報告"

    def test_unknown_intent_returns_none_triple(self) -> None:
        out = _IntentOut(intent="unknown", confidence="low", rationale=None)
        assert normalize_result(out) == (None, None, None)

    def test_garbage_intent_falls_to_unknown(self) -> None:
        out = _IntentOut(intent="world-domination", confidence="high", rationale="x")
        assert normalize_result(out) == (None, None, None)

    def test_invalid_confidence_defaults_to_low(self) -> None:
        out = _IntentOut(intent="espionage", confidence="very-high", rationale=None)
        intent, confidence, _ = normalize_result(out)
        assert intent == "espionage"
        assert confidence == "low"

    def test_long_rationale_is_truncated_to_80_chars(self) -> None:
        out = _IntentOut(intent="espionage", confidence="medium", rationale="あ" * 200)
        _, _, rationale = normalize_result(out)
        assert rationale is not None
        assert len(rationale) <= 80
