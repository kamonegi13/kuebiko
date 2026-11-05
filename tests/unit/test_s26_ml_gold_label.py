"""S26-ML-GOLD のラベルの段の純粋な部分 (Opus は呼ばない)。"""

from __future__ import annotations

from typing import Any

import pytest

from scripts.s26_ml_gold import core, label


def test_positive_definitions() -> None:
    assert label.positive("detect", {"open": True}, "")
    assert not label.positive("detect", {"open": False}, "")
    assert label.positive("deep_dive", {"need_to_know": 2, "urgency": 1, "depth": 2}, "")
    assert not label.positive("deep_dive", {"need_to_know": 3, "urgency": 2, "depth": 3}, "")
    assert label.positive("relation", {"label": "side"}, "")
    assert not label.positive("relation", {"label": "same_actor"}, "")


def test_build_inputs_rejects_changed_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(label, "_cut", lambda body: body)
    arts: dict[str, dict[str, Any]] = {
        "a1": {
            "title": "t",
            "feed_title": "f",
            "body": "本文",
            "published_at": "2026-10-28T00:00:00Z",
        }
    }
    row = {"set": "detect", "key": "a1", "body_sha256": {"a1": core.sha256_text("本文")}}
    got = label.build_inputs(row, arts)
    assert got == {"見出し": "t", "出典": "f", "公開日": "2026-10-28", "本文": "本文"}
    row_bad = {**row, "body_sha256": {"a1": core.sha256_text("別の本文")}}
    assert label.build_inputs(row_bad, arts) is None


def test_build_inputs_relation_uses_label_members(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(label, "_cut", lambda body: body)
    arts: dict[str, dict[str, Any]] = {
        "a": {"title": "ta", "body": "x"},
        "b": {"title": "tb", "body": None},
    }
    row = {
        "set": "relation",
        "key": "ev-1|ev-2",
        "label_members_a": ["a"],
        "label_members_b": ["b"],
        "body_sha256": {"a": core.sha256_text("x"), "b": core.sha256_text("")},
    }
    got = label.build_inputs(row, arts)
    assert got is not None
    assert [x["見出し"] for x in got["event_1"]] == ["ta"]
    assert got["event_2"][0]["本文"] == ""


def test_swap_fails_loudly_when_template_changed() -> None:
    assert label._swap("見出し: {title}", "見出し: {title}", "X") == "X"
    with pytest.raises(SystemExit):
        label._swap("別の形", "見出し: {title}", "X")


@pytest.mark.parametrize("name", ["detect", "deep_dive"])
def test_rubric_for_tracked_sources_matches_current_templates(name: str) -> None:
    rubric, question, schema, src = label.rubric_for(name)
    assert "要約: {summary}" not in rubric
    assert label.NOT_INSTRUCTION in rubric
    assert question and schema is not None and len(src) == 1
