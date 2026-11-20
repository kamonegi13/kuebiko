"""出力上限到達の信号 (2026-11-20、docs/synthesis_length_gate.md §2.1)。"""

from __future__ import annotations

import asyncio

from src.tools.llm_truncation import (
    note_hit_cap,
    note_observed,
    note_salvaged,
    watch_truncation,
)


def test_signal_is_none_outside_watch() -> None:
    note_hit_cap()  # 見張っていなければ何もしない (例外にしない)
    note_salvaged()


def test_watch_records_cap_and_salvage() -> None:
    with watch_truncation() as probe:
        assert not probe.truncated
        note_hit_cap()
        note_salvaged()
    assert probe.hit_cap and probe.salvaged and probe.truncated


def test_signal_reaches_through_child_task() -> None:
    async def child() -> None:
        note_hit_cap()

    async def main() -> bool:
        with watch_truncation() as probe:
            await asyncio.wait_for(child(), timeout=1)
        return probe.hit_cap

    assert asyncio.run(main()) is True


def test_nested_watch_does_not_leak_to_outer() -> None:
    with watch_truncation() as outer:
        with watch_truncation() as inner:
            note_hit_cap()
        assert inner.hit_cap
    assert not outer.hit_cap


def test_observed_marks_a_signalling_client() -> None:
    with watch_truncation() as probe:
        assert not probe.observed
        note_observed()
    assert probe.observed and not probe.truncated
