"""状況総括の教師収穫 (保存 estimate からの射影方式) の不変量。

- 台帳を書き換える経路 (``generate_synthesis(now=過去)``) を **import しない** — 2026-09-06 の
  収穫が revision 95 件・既読マーク 133 件を過去時刻で汚染した再発防止。
- 凍結審判の窓は収穫しない。
- 長さは収穫時に落とす (MLX の壁 ~14.1k tok)。予算超過は教師に投げない。
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import build_sft_teacher_synthesis as harvest_mod  # noqa: E402
from build_sft_teacher_synthesis import (  # noqa: E402
    Window,
    _est_tokens,
    accept_completion,
    harvest,
    reserved_keys,
    select_windows,
)

from src.storage.run_history import StatusSynthesisRecord  # noqa: E402
from src.synthesis.grounded.estimate import (  # noqa: E402
    Estimate,
    EvidenceItem,
    HypothesisScore,
    KeyJudgment,
    estimate_to_dict,
)


def _record(day: int, judgments: int = 3) -> StatusSynthesisRecord:
    start = datetime(2026, 8, day, 15, tzinfo=UTC)
    js = tuple(
        KeyJudgment(
            id=f"j{i}",
            claim=f"判定 {i}",
            domain="cyber_incident",
            leading_hypothesis="criminal_financial",
            confidence="moderate",
            confidence_basis="b",
            hypotheses=(
                HypothesisScore(
                    hypothesis="criminal_financial", consistent=1, inconsistent=0, verdict="leading"
                ),
            ),
            evidence=(
                EvidenceItem(
                    article_id="a",
                    source_tier="t",
                    attribution_basis="vendor_confirmed",
                    excerpt="e",
                ),
            ),
            delta_type="escalated",
        )
        for i in range(judgments)
    )
    est = Estimate(period_type="daily", period_start=start, period_end=start, judgments=js)
    return StatusSynthesisRecord(
        period_type="daily",
        period_start=start,
        period_end=start,
        headline="h",
        weight_section="",
        chain_section="",
        cog_section="",
        spillover_section="",
        pir_section="",
        axes_evidence="{}",
        tradecraft=json.dumps({"grounded_estimate": estimate_to_dict(est)}),
        article_count=0,
        llm_model=None,
        generated_at=start,
    )


class TestNoReplayPath:
    def test_harvest_never_imports_the_pipeline_replay(self) -> None:
        """generate_synthesis は台帳を書く経路。静的に断つ (import しない)。"""
        source = Path(harvest_mod.__file__).read_text(encoding="utf-8")
        assert "from src.synthesis.generator import generate_synthesis" not in source
        assert "generate_grounded_synthesis" not in source


class TestWindowSelection:
    def test_reserved_keys_and_recent_windows_are_excluded(self, tmp_path: Path) -> None:
        judge = tmp_path / "synthesis_judge_set.json"
        judge.write_text(json.dumps({"items": [{"key": "synth:daily:2026-08-02"}]}))
        records = [_record(1), _record(2), _record(20)]
        windows, skipped = select_windows(
            records,
            period_type="daily",
            reserved=reserved_keys([judge]),
            reserve_before=datetime(2026, 8, 10, tzinfo=UTC),
            cot=False,
        )
        assert [w.key for w in windows] == ["synth:daily:2026-08-01"]
        assert skipped["reserved_key"] == 1
        assert skipped["reserved_recent"] == 1

    def test_thin_windows_are_skipped_and_counted(self) -> None:
        windows, skipped = select_windows(
            [_record(1, judgments=1)],
            period_type="daily",
            reserved=set(),
            reserve_before=datetime(2026, 9, 1, tzinfo=UTC),
            cot=False,
        )
        assert windows == []
        assert skipped["thin"] == 1

    def test_cot_windows_carry_the_cot_instruction(self) -> None:
        windows, _ = select_windows(
            [_record(1)],
            period_type="daily",
            reserved=set(),
            reserve_before=datetime(2026, 9, 1, tzinfo=UTC),
            cot=True,
        )
        assert "analysis_notes" in windows[0].prompt


class TestBudget:
    def test_estimate_uses_measured_chars_per_token(self) -> None:
        assert 9_500 <= _est_tokens("あ" * 18_500) <= 10_500  # 実測比 1.85

    def test_pair_too_long_is_rejected(self) -> None:
        reason = accept_completion("x" * 20_000, "y" * 10_000, cot=False, max_pair_tokens=13_000)
        assert reason == "pair_too_long"

    def test_missing_notes_is_rejected_only_in_cot_mode(self) -> None:
        completion = json.dumps({"headline": "h" * 500})
        assert accept_completion("p", completion, cot=True, max_pair_tokens=13_000) == "no_notes"
        assert accept_completion("p", completion, cot=False, max_pair_tokens=13_000) is None


class _Teacher:
    def __init__(self) -> None:
        self.model = "teacher"
        self.calls = 0

    async def generate_structured(self, prompt: str, schema: type, **kw: Any) -> Any:
        self.calls += 1
        return schema(analysis_notes="点検メモ", headline="h" * 500)


class TestHarvestLoop:
    @pytest.mark.asyncio
    async def test_oversized_prompt_never_reaches_the_teacher(self, tmp_path: Path) -> None:
        teacher = _Teacher()
        stats = await harvest(
            teacher,
            [Window(key="k", prompt="x" * 100_000, judgments=3)],
            out=tmp_path / "o.jsonl",
            cot=True,
            max_prompt_tokens=10_500,
            max_pair_tokens=13_000,
            dry_run=False,
        )
        assert teacher.calls == 0
        assert stats["oversize"] == 1

    @pytest.mark.asyncio
    async def test_accepted_pair_is_written_and_resumable(self, tmp_path: Path) -> None:
        teacher = _Teacher()
        out = tmp_path / "o.jsonl"
        w = Window(key="k", prompt="短いプロンプト", judgments=3)
        await harvest(
            teacher,
            [w],
            out=out,
            cot=True,
            max_prompt_tokens=10_500,
            max_pair_tokens=13_000,
            dry_run=False,
        )
        stats = await harvest(
            teacher,
            [w],
            out=out,
            cot=True,
            max_prompt_tokens=10_500,
            max_pair_tokens=13_000,
            dry_run=False,
        )
        assert teacher.calls == 1  # 2 回目は done として飛ばす
        assert stats["done"] == 1
        row = json.loads(out.read_text().splitlines()[0])
        assert json.loads(row["completion"])["analysis_notes"] == "点検メモ"

    @pytest.mark.asyncio
    async def test_dry_run_calls_nothing(self, tmp_path: Path) -> None:
        teacher = _Teacher()
        stats = await harvest(
            teacher,
            [Window(key="k", prompt="p", judgments=3)],
            out=tmp_path / "o.jsonl",
            cot=True,
            max_prompt_tokens=10_500,
            max_pair_tokens=13_000,
            dry_run=True,
        )
        assert teacher.calls == 0
        assert stats["ok"] == 1
