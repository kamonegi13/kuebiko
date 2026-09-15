"""常設情報要求の面 API (PIR ブリーフ 段A) の不変量。

**期間の産物ではない**ことが構造要件 — 窓の Query を持たず、「いま時点の答え」と
「前回から何がどう動いたか」を返す (docs/pir_brief_design.md §1)。
"""

from __future__ import annotations

import inspect
from typing import Any

from src.ui.api.questions import _summarize, list_questions


class TestSummary:
    """冒頭の 1 行。静穏日に「動いていない」と言えることが要件。"""

    def _q(self, **over: Any) -> dict[str, Any]:
        return {"assessed": True, "delta_type": "no_change", **over}

    def test_counts_moved_answers(self) -> None:
        questions = [
            self._q(delta_type="weakened"),
            self._q(delta_type="hypothesis_flip"),
            self._q(),
        ]

        assert _summarize(questions) == {
            "total": 3,
            "assessed": 3,
            "moved": 2,
            "unassessed": 0,
        }

    def test_quiet_day_reports_zero_moved(self) -> None:
        """答えが動かない日も「0 問が動いた」と言える (省略しない)。"""
        assert _summarize([self._q(), self._q()])["moved"] == 0

    def test_unassessed_questions_are_counted_separately(self) -> None:
        """証拠が無く未評価の問いを「動いていない」に混ぜない (別の状態)。"""
        summary = _summarize([self._q(), {"assessed": False, "delta_type": ""}])

        assert summary["assessed"] == 1
        assert summary["unassessed"] == 1
        assert summary["moved"] == 0


class TestNoWindow:
    def test_endpoint_takes_no_period_parameter(self) -> None:
        """窓を取らないことが SIR 側の配信物との構造的な違い (状態 vs 期間)。"""
        params = set(inspect.signature(list_questions).parameters)

        assert params == set()
