"""detect (台帳の開設候補) の SFT 教師収穫 — 日の選定と対の形 (2026-09-21)。

⚠ 凍結評価に使った replay の 5 日は収穫しない (生徒が答えを見て答える)。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from scripts.build_sft_teacher_detect import (
    EVAL_HOLDOUT_DAYS,
    RecordingClient,
    plan_days,
    teacher_row,
)


class TestPlanDays:
    def test_excludes_holdout_and_done_days_within_range(self) -> None:
        days = plan_days(
            since="2026-08-17",
            until="2026-08-22",
            exclude=frozenset({"2026-08-18", "2026-08-20"}),
            done=frozenset({"2026-08-19:cur"}),
        )
        assert days == ["2026-08-17", "2026-08-21", "2026-08-22"]

    def test_default_holdout_is_the_frozen_replay_days(self) -> None:
        assert (
            frozenset({"2026-08-18", "2026-08-20", "2026-08-31", "2026-09-02", "2026-09-03"})
            == EVAL_HOLDOUT_DAYS
        )

    def test_newest_first_when_requested(self) -> None:
        assert plan_days(
            since="2026-09-01", until="2026-09-03", exclude=frozenset(), newest_first=True
        ) == [
            "2026-09-03",
            "2026-09-02",
            "2026-09-01",
        ]


class _Out(BaseModel):
    open: list[dict[str, str]] = [{"claim": "x"}]
    rejected: list[dict[str, str]] = []


class _Inner:
    model = "sonnet"

    async def generate_structured(self, prompt: str, schema: Any, **kw: Any) -> _Out:
        return _Out()


class TestRecordingClient:
    def test_captures_prompt_and_completion_verbatim(self) -> None:
        import asyncio

        rec = RecordingClient(_Inner())  # type: ignore[arg-type]
        asyncio.run(rec.generate_structured("P", _Out))

        assert rec.last is not None
        assert rec.last[0] == "P"
        assert json.loads(rec.last[1])["open"] == [{"claim": "x"}]

    def test_row_keeps_prompt_completion_and_counts(self) -> None:
        row = teacher_row(
            day="2026-08-17",
            arm="cur",
            template="synthesis/detect_new.j2",
            prompt="P",
            completion='{"open": [{"claim": "x", "article_ids": ["a"]}], "rejected": [{"a": 1}]}',
            n_candidates=5,
        )
        assert row["key"] == "2026-08-17:cur"
        assert row["prompt"] == "P" and row["n_candidates"] == 5
        assert row["n_open"] == 1 and row["n_rejected"] == 1
