"""積み重ね型 event → standing の移行 (2026-09-19、T3)。

不変条件: 資格要件を満たさない計画は書かない / 証拠は評価済みだけ移す / 元は closed にする /
revision は移さない (問いが変わる以上、答えの履歴は引き継げない)。
"""

from __future__ import annotations

from typing import Any

import pytest

from scripts.migrate_event_to_standing import draft_of, load_plan, plan_report


class _FakeStore:
    def __init__(self, evidence: dict[str, list[str]]) -> None:
        self._ev = evidence
        self.closed: list[str] = []
        self.assigned: list[tuple[str, str]] = []

    def get_situation(self, situation_id: str) -> Any:
        return object() if situation_id in self._ev else None

    def evidence_items(self, situation_id: str, *, limit: int = 5) -> list[dict[str, str]]:
        return [{"article_id": a} for a in self._ev.get(situation_id, [])][:limit]

    def latest_revision(self, situation_id: str) -> Any:
        return None

    def record_assignment(self, **kw: Any) -> bool:
        self.assigned.append((str(kw["situation_id"]), str(kw["article_id"])))
        return True

    def touch_situation(self, situation_id: str, **kw: Any) -> None:
        if kw.get("status") == "closed":
            self.closed.append(situation_id)


_ITEM: dict[str, Any] = {
    "from": ["s-a", "s-b"],
    "frame_id": "escalation",
    "slots": {"subject": "us", "counterpart": "ir", "stage": "military"},
    "decision": "次の段階に備えるか",
    "answer_can_move_ack": True,
    "evidence_condition": {"any": []},
}


def test_plan_report_counts_sources_without_writing() -> None:
    store = _FakeStore({"s-a": ["x", "y"], "s-b": ["z"]})
    rep = plan_report(_ITEM, store)  # type: ignore[arg-type]  # 口だけの偽 store
    assert rep["qualified"] is True
    assert rep["question"] == "米国とイランの対立は直接的な武力行使の段階へ進むか"
    assert rep["evidence_assessed"] == {"s-a": 2, "s-b": 1}
    assert store.closed == [] and store.assigned == []


def test_plan_report_reports_failures_instead_of_raising() -> None:
    bad = {**_ITEM, "decision": "", "answer_can_move_ack": False}
    rep = plan_report(bad, _FakeStore({"s-a": []}))  # type: ignore[arg-type]
    assert rep["qualified"] is False
    assert len(rep["failures"]) >= 2
    assert rep["question"] == "(資格要件を満たさない)"


def test_draft_of_maps_plan_fields() -> None:
    d = draft_of(_ITEM)
    assert d.frame_id == "escalation" and d.slots["counterpart"] == "ir"
    assert d.answer_can_move_ack is True


def test_load_plan_rejects_non_list(tmp_path: Any) -> None:
    p = tmp_path / "p.json"
    p.write_text('{"from": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="配列"):
        load_plan(p)
