"""子の持ち時間 = パイプラインの上限と呼び手の締め切りの残りの短い方 (2026-09-30)。"""

from __future__ import annotations

from src.ui.services.pipeline_runner import effective_timeout


def test_no_deadline_keeps_pipeline_timeout() -> None:
    assert effective_timeout(1800.0, None, now=100.0) == 1800.0


def test_chain_step_deadline_shortens_the_budget() -> None:
    """段の 720 秒の中で、鍵を 30 秒待ってから始まった子は残り (−余白) を持ち時間にする。"""
    started = 1000.0
    budget = effective_timeout(1800.0, deadline=started + 720.0, now=started + 30.0)

    assert budget == 720.0 - 30.0 - 15.0


def test_budget_never_below_floor() -> None:
    assert effective_timeout(1800.0, deadline=100.0, now=99.0) == 60.0


def test_pipeline_timeout_wins_when_shorter() -> None:
    assert effective_timeout(300.0, deadline=10_000.0, now=0.0) == 300.0
