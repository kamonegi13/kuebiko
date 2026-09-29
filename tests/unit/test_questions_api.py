"""常設情報要求の面 API (PIR ブリーフ 段A) の不変量。

**期間の産物ではない**ことが構造要件 — 窓の Query を持たず、「いま時点の答え」と
「前回から何がどう動いたか」を返す (docs/pir_brief_design.md §1)。
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import Any

from src.ui.api.questions import _summarize, list_questions

NOW = datetime(2026, 9, 29, tzinfo=UTC)
RECENT = "2026-09-28T21:00:00+00:00"
OLD = "2026-09-23T18:00:00+00:00"


class TestSummary:
    """冒頭の 1 行。静穏日に「動いていない」と言えることが要件。"""

    def _q(
        self, sid: str, delta: str = "no_change", at: str = RECENT, **over: Any
    ) -> dict[str, Any]:
        return {
            "situation_id": sid,
            "assessed": True,
            "delta_type": delta,
            "trajectory": [{"at": at, "delta_type": delta, "reason": ""}],
            **over,
        }

    def test_counts_moved_answers(self) -> None:
        questions = [self._q("a", "weakened"), self._q("b", "hypothesis_flip"), self._q("c")]

        summary = _summarize(questions, now=NOW)

        assert (summary["total"], summary["assessed"], summary["moved"]) == (3, 3, 2)
        assert summary["moved_ids"] == ["a", "b"]

    def test_old_move_is_not_counted(self) -> None:
        """最新の改訂が動きでも、24 時間より前なら今日は動いていない。"""
        assert _summarize([self._q("a", "claim_revised", at=OLD)], now=NOW)["moved"] == 0

    def test_quiet_day_reports_zero_moved(self) -> None:
        """答えが動かない日も「0 問が動いた」と言える (省略しない)。"""
        assert _summarize([self._q("a"), self._q("b")], now=NOW)["moved"] == 0

    def test_unassessed_questions_are_counted_separately(self) -> None:
        """証拠が無く未評価の問いを「動いていない」に混ぜない (別の状態)。"""
        summary = _summarize([self._q("a"), {"assessed": False, "delta_type": ""}], now=NOW)

        assert summary["assessed"] == 1
        assert summary["unassessed"] == 1
        assert summary["moved"] == 0


class TestNoWindow:
    def test_endpoint_takes_no_period_parameter(self) -> None:
        """窓を取らないことが SIR 側の配信物との構造的な違い (状態 vs 期間)。"""
        params = set(inspect.signature(list_questions).parameters)

        assert params == set()
