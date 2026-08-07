"""routing rule 発火監査のテスト (2026-07-18)。

核心: (1) 発火 0 の週がゼロ充填され死亡を検知できる、(2) share ベースで量変動に
頑健、(3) rare rule は急落監視から除外、(4) enabled ルールの発火実績ゼロ検知
(R0 病理 = UI 編集ルールの空振り)。
"""

from __future__ import annotations

from datetime import date

from src.ui.services.routing_rule_audit import (
    RuleWeekCell,
    bucket_rule_weeks,
    build_rule_section,
    detect_rule_warns,
)

_W1 = date(2026, 6, 15)  # 月曜
_W2 = date(2026, 6, 22)
_W3 = date(2026, 6, 29)
_W4 = date(2026, 7, 6)
_EVAL = date(2026, 7, 13)
_WEEKS = (_W1, _W2, _W3, _W4, _EVAL)


def _cells(rule_id: str, shares: list[float], posted: int = 1000) -> list[RuleWeekCell]:
    return [
        RuleWeekCell(
            rule_id=rule_id,
            week_start=wk,
            fired=int(posted * s / 100),
            posted=posted,
        )
        for wk, s in zip(_WEEKS, shares, strict=True)
    ]


class TestBucketZeroFill:
    def test_missing_week_is_zero_filled(self) -> None:
        """発火 0 の週にもセルが立つ (group-by の構造的盲点の回避 = 死亡検知の要)。"""
        posted = [("2026-07-06", 500), ("2026-07-13", 500)]
        rule_daily = [("2026-07-06", "R1.x", 50)]  # eval 週 (07-13) は発火なし
        cells = bucket_rule_weeks(rule_daily, posted)
        assert [c.fired for c in cells["R1.x"]] == [50, 0]
        assert cells["R1.x"][1].posted == 500

    def test_share_computed_from_weekly_posted(self) -> None:
        cells = bucket_rule_weeks([("2026-07-07", "R1.x", 100)], [("2026-07-07", 1000)])
        assert cells["R1.x"][0].share == 10.0


class TestDetectWarns:
    def test_dead_rule_detected_as_collapse(self) -> None:
        # 4 週 10% → 前週 0% (発火消滅)
        got = detect_rule_warns({"R1.x": _cells("R1.x", [10, 10, 10, 10, 0])}, [], eval_week=_EVAL)
        assert len(got) == 1
        assert got[0].kind == "collapse"
        assert got[0].current_fired == 0

    def test_share_halved_detected(self) -> None:
        got = detect_rule_warns({"R1.x": _cells("R1.x", [10, 12, 10, 11, 4])}, [], eval_week=_EVAL)
        assert len(got) == 1 and got[0].kind == "collapse"

    def test_healthy_rule_no_warn(self) -> None:
        got = detect_rule_warns({"R1.x": _cells("R1.x", [10, 12, 10, 11, 9])}, [], eval_week=_EVAL)
        assert got == []

    def test_volume_swing_does_not_false_positive(self) -> None:
        """収集量が半減しても share が保たれていれば警告しない (share 判定の頑健性)。"""
        cells = [
            RuleWeekCell(rule_id="R1.x", week_start=wk, fired=100, posted=1000)
            for wk in (_W1, _W2, _W3, _W4)
        ] + [RuleWeekCell(rule_id="R1.x", week_start=_EVAL, fired=40, posted=400)]
        got = detect_rule_warns({"R1.x": cells}, [], eval_week=_EVAL)
        assert got == []

    def test_rare_rule_exempt_from_collapse(self) -> None:
        # 中央値 0.5% (< floor 1%) の希少ルールは急落監視しない
        got = detect_rule_warns(
            {"R8.rare": _cells("R8.rare", [0.5, 0.5, 0.5, 0.5, 0])}, [], eval_week=_EVAL
        )
        assert got == []

    def test_small_week_not_judged(self) -> None:
        got = detect_rule_warns(
            {"R1.x": _cells("R1.x", [10, 10, 10, 10, 0], posted=10)}, [], eval_week=_EVAL
        )
        assert got == []

    def test_never_fired_enabled_rule(self) -> None:
        """R0 病理: enabled なのに全 lookback で発火ゼロ (発火行が無い) を検知。"""
        got = detect_rule_warns(
            {"R1.x": _cells("R1.x", [10, 10, 10, 10, 10])},
            ["R1.x", "R0.ghost"],
            eval_week=_EVAL,
        )
        assert [w.rule_id for w in got] == ["R0.ghost"]
        assert got[0].kind == "never_fired"

    def test_all_zero_cells_counts_as_never_fired(self) -> None:
        got = detect_rule_warns(
            {"R9.z": _cells("R9.z", [0, 0, 0, 0, 0])}, ["R9.z"], eval_week=_EVAL
        )
        assert len(got) == 1 and got[0].kind == "never_fired"


class TestReportSection:
    def test_ok_line(self) -> None:
        assert build_rule_section([], rules_checked=13) == ["ルール発火: 全 13 ルール OK"]

    def test_warn_lines_have_rule_and_reason(self) -> None:
        from src.ui.services.routing_rule_audit import RuleWarn

        lines = build_rule_section(
            [
                RuleWarn(
                    "R2.x", "collapse", baseline_share=5.0, current_share=1.0, current_fired=8
                ),
                RuleWarn("R0.ghost", "never_fired"),
            ],
            rules_checked=13,
        )
        assert any("R2.x" in ln and "5.0%" in ln for ln in lines)
        assert any("R0.ghost" in ln and "発火実績なし" in ln for ln in lines)


class TestDisabledRuleSuppression:
    """意図的に無効化されたルールの発火消滅は警告しない (2026-07-18)。"""

    def test_disabled_rule_collapse_suppressed(self) -> None:
        # データ上は急落だが enabled 集合に居ない → 意図的無効化とみなし警告なし
        got = detect_rule_warns(
            {"R6.dead": _cells("R6.dead", [10, 10, 10, 10, 0])},
            ["R1.other"],  # R6.dead は enabled に含まれない
            eval_week=_EVAL,
        )
        assert [w.rule_id for w in got] == ["R1.other"]  # never_fired のみ (R6 の collapse なし)
        assert got[0].kind == "never_fired"

    def test_config_load_failure_fails_open(self) -> None:
        # enabled 空 (config ロード失敗の縮退) では急落監視を止めない
        got = detect_rule_warns(
            {"R6.dead": _cells("R6.dead", [10, 10, 10, 10, 0])}, [], eval_week=_EVAL
        )
        assert len(got) == 1 and got[0].kind == "collapse"
