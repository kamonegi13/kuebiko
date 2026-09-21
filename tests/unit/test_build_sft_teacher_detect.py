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
    select_active_as_of,
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


class TestActiveTitlesAsOf:
    """⚠ 追跡中の情勢は **その日時点** のものを渡す (2026-09-21)。

    収穫時点の台帳を渡すと、6 月の日に「7 月以降に立った 147 件が既に追跡中」と告げて
    「これらと同じ事象は選ぶな」と指示することになる (実測: 現在 active な 147 件の
    開設は 7 月 82 / 8 月 31 / 9 月 34 で、6 月は 0 件)。プロンプトの 63% を占める
    ブロックでもあり、as-of にすると 6-7 月の対は壁 (14.1k tok) の内側へ入る。
    """

    def test_situations_opened_after_the_day_are_excluded(self) -> None:
        rows = [
            {"title": "古い情勢", "opened_at": "2026-05-01T00:00:00+00:00", "closed_at": None},
            {"title": "未来の情勢", "opened_at": "2026-07-10T00:00:00+00:00", "closed_at": None},
        ]

        assert select_active_as_of(rows, day="2026-06-15") == ["古い情勢"]

    def test_situations_closed_before_the_day_are_excluded(self) -> None:
        rows = [
            {
                "title": "閉じた情勢",
                "opened_at": "2026-05-01T00:00:00+00:00",
                "closed_at": "2026-06-01T00:00:00+00:00",
            },
            {
                "title": "閉じたのは後",
                "opened_at": "2026-05-01T00:00:00+00:00",
                "closed_at": "2026-07-01T00:00:00+00:00",
            },
        ]

        assert select_active_as_of(rows, day="2026-06-15") == ["閉じたのは後"]

    def test_dormant_rows_count_because_they_were_active_then(self) -> None:
        """⭐ 現在の status では絞らない — 今 dormant でも当時は追跡中だった。"""
        rows = [{"title": "休眠中", "opened_at": "2026-06-01T00:00:00+00:00", "closed_at": None}]

        assert select_active_as_of(rows, day="2026-06-15") == ["休眠中"]

    def test_the_day_itself_is_included_up_to_its_end(self) -> None:
        rows = [{"title": "当日開設", "opened_at": "2026-06-15T18:00:00+00:00", "closed_at": None}]

        assert select_active_as_of(rows, day="2026-06-15") == ["当日開設"]
