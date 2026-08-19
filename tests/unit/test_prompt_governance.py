"""プロンプト統治 週次監査 (scheduler ラッパ) の純粋部分のテスト。

判定ロジックの SSoT は script 側 (audit_prompt_prohibitions / verify_prompt_cutover) に
あり、ここでは「窓の clamp」「総合行の抽出」「VIOLATED 行の抜き出し」だけを固定する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.ui.services.prompt_governance import (
    _summary_line,
    _violated_lines,
    clamp_window_days,
)

_NOW = datetime(2026, 8, 24, 8, 20, tzinfo=UTC)


class TestClampWindowDays:
    def test_under_one_day_returns_none(self) -> None:
        # 切替直後は post 窓に標本が無い — 判定せず次週へ (verify を空振りさせない)
        assert clamp_window_days(_NOW, _NOW - timedelta(hours=23)) is None

    def test_elapsed_days_are_used_up_to_the_calibrated_cap(self) -> None:
        assert clamp_window_days(_NOW, _NOW - timedelta(days=1, hours=2)) == 1
        assert clamp_window_days(_NOW, _NOW - timedelta(days=4, hours=6)) == 4
        # 較正は 7 日窓で行われているため 7 で頭打ち
        assert clamp_window_days(_NOW, _NOW - timedelta(days=30)) == 7


class TestOutputParsing:
    def test_summary_line_picks_the_status_line(self) -> None:
        out = "ヘッダ\n表の行 1\n表の行 2\n\n総合: INCONCLUSIVE\n"
        assert _summary_line(out, prefix="総合:") == "総合: INCONCLUSIVE"

    def test_summary_line_falls_back_to_last_line(self) -> None:
        assert _summary_line("何か壊れた出力\n", prefix="総合判定:") == "何か壊れた出力"
        assert _summary_line("", prefix="総合:") == "(出力なし)"

    def test_violated_lines_exclude_the_summary_itself(self) -> None:
        out = (
            "非サイバーの victim_org   —   87   4  VIOLATED\n"
            "何かの OK 行             gate  99   0  OK\n"
            "総合: VIOLATED\n"
        )
        rows = _violated_lines(out)
        assert len(rows) == 1
        assert rows[0].startswith("非サイバー")
