"""DETECT-JUDGE 採点の純粋部分 (scripts/gpu_window/detect_judge_core.py)。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "gpu_window"))
import detect_judge_core as jc  # noqa: E402


def _rec(window: str, arm: str, claims: list[tuple[list[str], str]], **kw: Any) -> dict[str, Any]:
    opened = [{"article_ids": ids, "claim": c} for ids, c in claims]
    base = {
        "window_id": window,
        "arm": arm,
        "opened": opened,
        "n_opened": len(opened),
        "fallback": False,
        "variant": "decompose" if arm == "ship" else "current",
        "n_gate_blocked": 0,
    }
    return {**base, **kw}


def test_unit_id_ignores_article_order() -> None:
    assert jc.unit_id("w", ["b", "a"], "c") == jc.unit_id("w", ["a", "b"], "c")
    assert jc.unit_id("w", ["a"], "c") != jc.unit_id("w", ["a"], "d")


def test_collect_units_folds_runs_and_skips_fallback() -> None:
    recs = [
        _rec("w1", "ship", [(["a"], "x")]),
        _rec("w1", "ship", [(["a"], "x")], run=2),
        _rec("w1", "current", [(["a"], "x")]),
        _rec("w1", "ship", [(["z"], "q")], fallback=True),
    ]
    units = jc.collect_units(recs)
    assert len(units) == 1
    assert next(iter(units.values())).arms == ("current", "ship")


def test_allocate_proportional_and_bounded() -> None:
    assert jc.allocate({"a": 344, "b": 282}, 260) == {"a": 143, "b": 117}
    assert jc.allocate({"a": 3, "b": 2}, 260) == {"a": 3, "b": 2}


def test_sample_is_deterministic_with_weights() -> None:
    recs = [_rec("w", "ship" if i % 2 else "current", [([f"a{i}"], f"c{i}")]) for i in range(10)]
    units = jc.collect_units(recs)
    s1, w1 = jc.sample_units(units, total=4)
    s2, _ = jc.sample_units(units, total=4)
    assert s1 == s2 and len(s1) == 4
    assert all(abs(v - 5 / 2) < 1e-9 for v in w1.values()) or sum(w1.values()) > 0
    full, wf = jc.sample_units(units, total=100)
    assert len(full) == 10 and set(wf.values()) == {1.0}


def test_combine_rules() -> None:
    assert jc.combine("yes", "yes", "yes") == jc.CORRECT
    assert jc.combine("yes", "no", "undecidable") == jc.WRONG
    assert jc.combine("yes", "undecidable", "yes") == jc.UNDECIDED


def test_wilcoxon_and_sign() -> None:
    assert jc.wilcoxon_p([0, 0]) == 1.0
    # n=5 すべて正: 厳密 p = 2/32
    assert abs(jc.wilcoxon_p([1, 2, 3, 4, 5]) - 2 / 32) < 1e-12
    assert abs(jc.sign_test_p([1, 1, 1, 1, 1, 0]) - 2 / 32) < 1e-12
    assert jc.sign_test_p([1, -1]) == 1.0


def test_kappa() -> None:
    assert jc.cohen_kappa(["a", "b", "a"], ["a", "b", "a"]) == 1.0
    assert jc.cohen_kappa([], []) is None


def test_window_values_impute_and_analyze() -> None:
    recs = []
    for w in ("w1", "w2", "w3"):
        recs.append(_rec(w, "current", [([f"{w}c"], "cc")]))
        recs.append(_rec(w, "ship", [([f"{w}s"], "ss"), ([f"{w}t"], "tt")]))
    units = jc.collect_units(recs)
    sampled = [u for u in units if units[u].claim in ("cc", "ss")]
    verdicts = dict.fromkeys(sampled, jc.CORRECT)
    rates = jc.arm_rates(units, sampled, verdicts)
    assert rates["ship"] == (1.0, 0.0)
    vals = jc.window_arm_values(recs, units, verdicts, set(sampled), rates, set())
    assert vals[("w1", "ship")] == (2.0, 0.0)  # 抜かれた tt は割合 1.0 で補う
    stats = jc.analyze(vals, iterations=200)
    assert stats["n_windows"] == 3 and stats["mean_d"] == 1.0
    assert stats["mean_d_ci"][0] <= 1.0 <= stats["mean_d_ci"][1]


def test_decide_pass_fail_undetermined() -> None:
    stats = {
        "n_windows": 20,
        "mean_d_ci": (-0.2, 0.5),
        "wilcoxon_p": 0.5,
        "median_d": 0.1,
        "false_rate_diff_ci": (-0.1, 0.02),
    }
    p3 = {"denominator": 100, "share": 0.1}
    p4 = {"share": 1.0}
    p5 = {"ratio": 0.9}
    assert jc.decide(stats, p3, p4, p5, 0.9)["verdict"] == "pass"
    assert jc.decide({**stats, "mean_d_ci": (-0.6, 0.5)}, p3, p4, p5, 0.9)["verdict"] == "fail"
    assert (
        jc.decide(stats, {"denominator": 10, "share": 0.1}, p4, p5, 0.9)["verdict"]
        == "undetermined"
    )
    assert jc.decide(stats, p3, p4, p5, 0.5)["verdict"] == "undetermined"
    worse = {**stats, "wilcoxon_p": 0.01, "median_d": -0.3}
    assert jc.decide(worse, p3, p4, p5, 0.9)["checks"]["P1"] is False


def test_p3_p4_p5() -> None:
    recs = [
        _rec("w", "ship", [(["a"], "x")], n_gate_blocked=1),
        _rec("w", "current", [(["a"], "y")]),
    ]
    assert jc.p3_share(recs)["share"] == 0.5
    assert jc.p4_share(recs)["share"] == 1.0
    assert jc.p5_ratio(recs)["ratio"] == 1.0
