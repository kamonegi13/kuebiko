"""台帳の割当に埋込の確認を課す関門 (2026-09-24)。

⚠ **発端**: 規則 (anchor/nation/token) による割当を Opus 盲検 362 組で測ると、「同じ情勢」は
anchor 31% / nation 10% / token 4% — 台帳の証拠の約 8 割が別事案か無関係だった。記事の要約と
情勢の題名の類似度 0.6 は、どの claim 種別でも「無関係」をほぼ全て落とし (2/132)、「同じ」の
88% を残す。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.assessment.assign_gate import (
    ASSIGN_GATE_THRESHOLD,
    GateResult,
    evaluate_gate,
    gate_mode,
)


def _v(*xs: float) -> np.ndarray:
    a = np.asarray(xs, dtype=np.float32)
    return a / np.linalg.norm(a)


class TestGateMode:
    def test_default_is_shadow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """既定は shadow — 落ちるはずの割当を記録するだけで、本番の割当は変えない。"""
        monkeypatch.delenv("ASSIGN_EMBED_GATE", raising=False)

        assert gate_mode() == "shadow"

    @pytest.mark.parametrize("raw", ["off", "shadow", "on"])
    def test_explicit_modes(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("ASSIGN_EMBED_GATE", raw)

        assert gate_mode() == raw

    def test_unknown_value_falls_back_to_shadow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ASSIGN_EMBED_GATE", "enforce")

        assert gate_mode() == "shadow"


class TestEvaluateGate:
    def test_similar_pair_passes(self) -> None:
        r = evaluate_gate(article_vec=_v(1, 0.1), situation_vec=_v(1, 0))

        assert r.passed is True
        assert r.cos is not None and r.cos >= ASSIGN_GATE_THRESHOLD

    def test_dissimilar_pair_is_dropped(self) -> None:
        r = evaluate_gate(article_vec=_v(1, 0), situation_vec=_v(0, 1))

        assert r.passed is False

    def test_missing_vector_keeps_the_assignment(self) -> None:
        """⚠ 埋込が無ければ **割当を残す** (確認できないことを理由に挙動を変えない)。"""
        r = evaluate_gate(article_vec=None, situation_vec=_v(1, 0))

        assert r == GateResult(cos=None, passed=True)

    def test_threshold_is_inclusive(self) -> None:
        a = _v(1, 0)
        s = _v(ASSIGN_GATE_THRESHOLD, float(np.sqrt(1 - ASSIGN_GATE_THRESHOLD**2)))

        assert evaluate_gate(article_vec=a, situation_vec=s).passed is True


class TestApplyGateInLedger:
    """台帳の割当の直後に関門を通す (stateful._apply_assign_gate)。"""

    @staticmethod
    def _run(monkeypatch: pytest.MonkeyPatch, mode: str, *, fail: bool = False):  # type: ignore[no-untyped-def]
        import asyncio
        from types import SimpleNamespace

        from src.assessment import stateful

        monkeypatch.setenv("ASSIGN_EMBED_GATE", mode)
        vecs = {"近い記事": _v(1, 0.1), "遠い記事": _v(0, 1), "情勢の題名": _v(1, 0)}

        async def fake_embed(texts):  # type: ignore[no-untyped-def]
            if fail:
                raise RuntimeError("埋込不可")
            return [vecs.get(t.split("\n")[0]) for t in texts]

        monkeypatch.setattr(stateful, "_embed_texts", fake_embed)
        repo = SimpleNamespace(load_summary_embeddings=lambda ids: {})
        pool_by_id = {
            "a1": {"article_id": "a1", "title": "近い記事", "summary": ""},
            "a2": {"article_id": "a2", "title": "遠い記事", "summary": ""},
        }
        titles = {"s1": "情勢の題名"}
        return asyncio.run(
            stateful._apply_assign_gate(  # noqa: SLF001
                new_by_sid={"s1": ["a1", "a2"]},
                assigned_by_aid={"a1": "token", "a2": "token"},
                unassigned=[],
                pool_by_id=pool_by_id,
                situation_titles=titles,
                repo=repo,  # type: ignore[arg-type]
            )
        )

    def test_shadow_keeps_every_assignment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        new_by_sid, assigned_by, unassigned = self._run(monkeypatch, "shadow")

        assert new_by_sid == {"s1": ["a1", "a2"]}
        assert unassigned == []

    def test_on_moves_dropped_articles_back_to_unassigned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        new_by_sid, assigned_by, unassigned = self._run(monkeypatch, "on")

        assert new_by_sid == {"s1": ["a1"]}
        assert "a2" not in assigned_by
        assert [a["article_id"] for a in unassigned] == ["a2"]

    def test_embedding_failure_keeps_assignments(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """関門の失敗で台帳の挙動を変えない。"""
        new_by_sid, _, unassigned = self._run(monkeypatch, "on", fail=True)

        assert new_by_sid == {"s1": ["a1", "a2"]}
        assert unassigned == []

    def test_off_does_not_embed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        new_by_sid, _, _ = self._run(monkeypatch, "off", fail=True)

        assert new_by_sid == {"s1": ["a1", "a2"]}
