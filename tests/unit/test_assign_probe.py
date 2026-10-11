"""台帳の割当判定 ML への probe 特徴量 (F2b、2026-10-11) のテスト。

``probe_choice`` 自体は tests/unit/test_decision_probe.py で検証済みなので AsyncMock で
差し替え、問いの組み立て・logit 変換・配線だけをここで検証する (thin_body_probe と同型)。
"""

from __future__ import annotations

import math
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.assessment.assign_probe import (
    ASSIGN_PROBE_FEATURE_ENV,
    assign_probe_feature_enabled,
    assign_probe_question,
    probe_assign_p_yes,
    probe_logit,
    sorted_strong_anchors,
)


def _cfg() -> Any:
    class _Cfg:
        ollama_base_url = "http://localhost:11434"

    return _Cfg()


class TestFlag:
    def test_default_is_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ASSIGN_PROBE_FEATURE_ENV, raising=False)

        assert assign_probe_feature_enabled() is False

    def test_zero_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ASSIGN_PROBE_FEATURE_ENV, "0")

        assert assign_probe_feature_enabled() is False

    def test_one_enables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ASSIGN_PROBE_FEATURE_ENV, "1")

        assert assign_probe_feature_enabled() is True


class TestProbeLogit:
    def test_matches_plain_logit_away_from_the_edges(self) -> None:
        assert probe_logit(0.5) == pytest.approx(0.0)
        assert probe_logit(0.9) == pytest.approx(math.log(0.9 / 0.1))

    def test_clips_near_zero_and_one_to_avoid_infinity(self) -> None:
        assert math.isfinite(probe_logit(0.0))
        assert math.isfinite(probe_logit(1.0))
        assert probe_logit(1.0) > probe_logit(0.999)


class TestSortedStrongAnchors:
    def test_filters_to_strong_types_and_sorts(self) -> None:
        anchors = frozenset(
            {"cve:CVE-2026-1", "mentioned_country:US", "actor:lazarus", "involved_country:JP"}
        )

        assert sorted_strong_anchors(anchors) == ["actor:lazarus", "cve:CVE-2026-1"]

    def test_caps_at_twelve(self) -> None:
        anchors = frozenset(f"cve:CVE-2026-{i}" for i in range(20))

        assert len(sorted_strong_anchors(anchors)) == 12


class TestAssignProbeQuestion:
    def test_includes_situation_and_article_text(self) -> None:
        q = assign_probe_question(
            situation_title="情勢の題名",
            anchors=frozenset({"cve:CVE-2026-1"}),
            article_title="記事の見出し",
            article_summary="記事の要約",
        )

        assert "情勢の題名" in q
        assert "CVE-2026-1" in q
        assert "記事の見出し" in q
        assert "記事の要約" in q
        assert "same" in q and "related" in q and "unrelated" in q

    def test_empty_anchors_shown_as_none(self) -> None:
        q = assign_probe_question(
            situation_title="t", anchors=frozenset(), article_title="a", article_summary="s"
        )

        assert "(なし)" in q

    def test_summary_truncated_to_600_chars(self) -> None:
        long_summary = "あ" * 1000
        q = assign_probe_question(
            situation_title="t",
            anchors=frozenset(),
            article_title="a",
            article_summary=long_summary,
        )

        assert "あ" * 600 in q
        assert "あ" * 601 not in q


class TestProbeAssignPYes:
    async def test_returns_p_yes_from_probe_choice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        probe = AsyncMock(return_value={"Yes": 0.8, "No": 0.2})
        monkeypatch.setattr("src.assessment.assign_probe.probe_choice", probe)

        result = await probe_assign_p_yes(
            situation_title="t",
            anchors=frozenset(),
            article_title="a",
            article_summary="s",
            config=_cfg(),
        )

        assert result == pytest.approx(0.8)
        probe.assert_awaited_once()

    async def test_none_passes_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        probe = AsyncMock(return_value=None)
        monkeypatch.setattr("src.assessment.assign_probe.probe_choice", probe)

        result = await probe_assign_p_yes(
            situation_title="t",
            anchors=frozenset(),
            article_title="a",
            article_summary="s",
            config=_cfg(),
        )

        assert result is None
