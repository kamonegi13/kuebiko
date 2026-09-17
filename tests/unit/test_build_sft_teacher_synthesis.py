"""教師収穫スクリプトの保存規則 — 有料で取った completion を捨てない (2026-09-16)。

1 窓目が pair_too_long で破棄され、外部枠を使った出力が消えた事故の再発防止。
不採用の対は副ファイルへ理由つきで保存し、再実行でも同じ窓を再課金しない。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.build_sft_teacher_synthesis import (
    Window,
    _done_keys,
    _oversize_path,
    accept_completion,
    harvest,
)


class _FakeSections:
    def __init__(self, payload: dict[str, str]) -> None:
        self._payload = payload

    def model_dump(self) -> dict[str, str]:
        return dict(self._payload)


class _FakeTeacher:
    """generate_structured だけを持つ教師。呼出回数を数える。"""

    def __init__(self, payload: dict[str, str]) -> None:
        self.calls = 0
        self._payload = payload

    async def generate_structured(self, *_args: Any, **_kwargs: Any) -> _FakeSections:
        self.calls += 1
        return _FakeSections(self._payload)


_LONG_NOTES = {"analysis_notes": "出典グラフ: " + "あ" * 600, "headline": "見出し" * 50}


def _run(teacher: _FakeTeacher, windows: list[Window], out: Path, **kw: Any) -> dict[str, int]:
    return asyncio.run(
        harvest(
            teacher,  # type: ignore[arg-type]  # 偽教師 (generate_structured のみ)
            windows,
            out=out,
            cot=True,
            max_prompt_tokens=kw.get("max_prompt_tokens", 10_500),
            max_pair_tokens=kw.get("max_pair_tokens", 13_000),
            dry_run=False,
        )
    )


def test_oversize_path_sits_next_to_main_output() -> None:
    assert _oversize_path(Path("data/mlx/teacher/synthesis.jsonl")) == Path(
        "data/mlx/teacher/synthesis_oversize.jsonl"
    )


def test_pair_too_long_completion_is_saved_to_sidecar_not_discarded(tmp_path: Path) -> None:
    # Arrange: prompt は単独で予算内、completion を足すと pair 上限を超える
    out = tmp_path / "synthesis.jsonl"
    window = Window(key="synth:daily:2026-08-29", prompt="p" * 1_000, judgments=3)
    teacher = _FakeTeacher(_LONG_NOTES)

    # Act
    stats = _run(teacher, [window], out, max_pair_tokens=700)

    # Assert
    assert stats["pair_too_long"] == 1 and stats["ok"] == 0
    assert out.read_text(encoding="utf-8") == ""
    rows = [
        json.loads(line) for line in _oversize_path(out).read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["key"] == window.key
    assert rows[0]["reason"] == "pair_too_long"
    assert json.loads(rows[0]["completion"])["analysis_notes"].startswith("出典グラフ")
    assert rows[0]["pair_tokens_est"] > 700


def test_sidecar_keys_count_as_done_so_rerun_does_not_pay_again(tmp_path: Path) -> None:
    # Arrange: 1 回目で副ファイルへ落ちた窓
    out = tmp_path / "synthesis.jsonl"
    window = Window(key="synth:daily:2026-08-29", prompt="p" * 1_000, judgments=3)
    teacher = _FakeTeacher(_LONG_NOTES)
    _run(teacher, [window], out, max_pair_tokens=700)
    assert teacher.calls == 1

    # Act: 同じ窓で再実行
    stats = _run(teacher, [window], out, max_pair_tokens=700)

    # Assert: 教師を呼ばず done に数える
    assert teacher.calls == 1
    assert stats["done"] == 1
    assert _done_keys(out) == {window.key}


def test_accepted_completion_goes_to_main_output_only(tmp_path: Path) -> None:
    out = tmp_path / "synthesis.jsonl"
    window = Window(key="synth:daily:2026-08-20", prompt="p" * 1_000, judgments=3)
    teacher = _FakeTeacher(_LONG_NOTES)

    stats = _run(teacher, [window], out)

    assert stats["ok"] == 1
    assert len(out.read_text(encoding="utf-8").splitlines()) == 1
    assert _oversize_path(out).read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    ("completion", "expected"),
    [
        ("x" * 100, "short"),
        (json.dumps({"analysis_notes": "", "headline": "h" * 500}), "no_notes"),
        (json.dumps({"analysis_notes": "n" * 500, "headline": "h" * 500}), None),
    ],
)
def test_accept_completion_reasons(completion: str, expected: str | None) -> None:
    assert accept_completion("p" * 100, completion, cot=True, max_pair_tokens=13_000) == expected


def test_subsample_estimate_keeps_subset_and_filters_relations() -> None:
    from datetime import UTC, datetime

    from scripts.build_sft_teacher_synthesis import subsample_estimate
    from src.synthesis.grounded.estimate import Estimate, KeyJudgment

    def j(i: int) -> KeyJudgment:
        return KeyJudgment(
            id=f"s-{i}",
            claim=f"c{i}",
            domain="d",
            leading_hypothesis="h",
            confidence="high",
            confidence_basis="b",
            hypotheses=(),
            evidence=(),
        )

    est = Estimate(
        period_type="daily",
        period_start=datetime(2026, 8, 1, tzinfo=UTC),
        period_end=datetime(2026, 8, 2, tzinfo=UTC),
        judgments=tuple(j(i) for i in range(10)),
        relations=(("s-0", "s-1", "same_actor", "x"), ("s-0", "s-9", "same_actor", "x")),
    )

    sub = subsample_estimate(est, keep_ratio=0.6, seed=1)
    again = subsample_estimate(est, keep_ratio=0.6, seed=1)

    assert sub is not None and len(sub.judgments) == 6
    kept = {x.id for x in sub.judgments}
    assert kept < {x.id for x in est.judgments}
    assert all(a in kept and b in kept for a, b, _, _ in sub.relations)
    assert again is not None and [x.id for x in again.judgments] == [x.id for x in sub.judgments]
    assert subsample_estimate(est, keep_ratio=1.0, seed=1) is None  # 落とせない窓は None


def test_subsample_keeps_minimum_judgments() -> None:
    from datetime import UTC, datetime

    from scripts.build_sft_teacher_synthesis import subsample_estimate
    from src.synthesis.grounded.estimate import Estimate, KeyJudgment

    js = tuple(
        KeyJudgment(
            id=f"s-{i}",
            claim="c",
            domain="d",
            leading_hypothesis="h",
            confidence="low",
            confidence_basis="b",
            hypotheses=(),
            evidence=(),
        )
        for i in range(3)
    )
    est = Estimate(
        period_type="daily",
        period_start=datetime(2026, 8, 1, tzinfo=UTC),
        period_end=datetime(2026, 8, 2, tzinfo=UTC),
        judgments=js,
    )
    sub = subsample_estimate(est, keep_ratio=0.1, seed=3)
    assert sub is not None and len(sub.judgments) == 2
