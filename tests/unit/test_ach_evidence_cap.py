"""ACH の証拠の件数の上限 (旗 ACH_EVIDENCE_CAP)。"""

from __future__ import annotations

from typing import Any

import pytest


class TestEvidenceCap:
    """ACH の証拠の件数の上限 (既定 off の旗 ACH_EVIDENCE_CAP、保存の前に重要度順で 6 件)。"""

    @staticmethod
    def _wire(n: int, polarity: str = "supports") -> list[Any]:
        from types import SimpleNamespace

        return [
            SimpleNamespace(
                index=1,
                article_id="a1",
                attribution_basis="unattributed",
                excerpt=f"x{i}",
                polarity=polarity,
            )
            for i in range(n)
        ]

    def _build(self, wire: list[Any]) -> tuple[Any, ...]:
        from src.synthesis.grounded.passes import build_evidence_items

        return build_evidence_items(
            wire,
            sources=[{"article_id": "a1", "feed_title": "f", "text": "t"}],
            tier_by_id={},
            log_context="s",
        )

    def test_off_by_default_keeps_everything(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ACH_EVIDENCE_CAP", raising=False)
        assert len(self._build(self._wire(9))) == 9

    def test_flag_caps_at_six(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ACH_EVIDENCE_CAP", "1")
        assert len(self._build(self._wire(9))) == 6
        assert len(self._build(self._wire(4))) == 4

    def test_neutral_is_dropped_before_supports_and_contradicts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ACH_EVIDENCE_CAP", "1")
        wire = self._wire(4, "neutral") + self._wire(3, "contradicts") + self._wire(3, "supports")
        kept = self._build(wire)
        assert len(kept) == 6
        assert all(e.polarity != "neutral" for e in kept)
