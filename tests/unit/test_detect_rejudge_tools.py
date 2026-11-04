"""DETECT-REJUDGE (docs/replay_prereg/detect_decompose_rejudge.md) の道具の純粋部分。"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS / "gpu_window"))
sys.path.insert(0, str(_SCRIPTS))

import detect_arms as da  # noqa: E402
import detect_closure_core as cc  # noqa: E402
import detect_common as dc  # noqa: E402
import detect_snapshot as ds  # noqa: E402

REJUDGE = dc.profile_named("rejudge")


def test_rejudge_profile_matches_prereg() -> None:
    assert REJUDGE.prereg_name == "detect_decompose_rejudge"
    assert REJUDGE.seed == 20261105
    assert REJUDGE.range_start == datetime(2026, 9, 24, tzinfo=UTC)
    assert REJUDGE.range_end == datetime(2026, 10, 26, 12, tzinfo=UTC)
    assert len(dc.build_bins(REJUDGE.range_start, REJUDGE.range_end)) == 130
    assert REJUDGE.as_of_strict and REJUDGE.check_ship_resolution
    assert "src/assessment/claim_fidelity.py" in REJUDGE.code_files


def test_rejudge_range_does_not_touch_prior_judge_range() -> None:
    prior = dc.profile_named("decompose")
    assert REJUDGE.range_end < prior.range_start


def test_default_profile_is_the_original_decompose() -> None:
    assert dc.PROFILE.name == "decompose"
    assert dc.SEED == 20261104 and dc.PREREG_NAME == "detect_decompose"
    assert all(a.writer_gates == "" for a in dc.ARMS)


def test_unknown_profile_is_rejected() -> None:
    with pytest.raises(SystemExit):
        dc.profile_named("nope")


def test_arm_env_sets_writer_gates_only_for_rejudge() -> None:
    current, ship = REJUDGE.arms
    assert dc.arm_env(current)["DETECT_WRITER_GATES"] == "0"
    assert dc.arm_env(ship) == {
        "DETECT_DECOMPOSE": "1",
        "LLM_USAGE_LOCAL": "0",
        "DETECT_WRITER_GATES": "1",
    }
    assert "DETECT_WRITER_GATES" not in dc.arm_env(dc.CURRENT_ARM)


def test_writer_gates_and_profile_flags_are_exempt_from_prod_match() -> None:
    prod = {"DETECT_WRITER_GATES": "0", "ASSIGN_EMBED_GATE": "on"}
    local = {"DETECT_WRITER_GATES": "1", "DETECT_GPU_PROFILE": "rejudge", "ASSIGN_EMBED_GATE": "on"}
    assert dc.flag_mismatches(prod, local) == []


def test_ship_model_check_refuses_other_resolution() -> None:
    ship = REJUDGE.arms[1]
    da.check_ship_model("kuebiko-sft:s24ci65", ship, required=True)
    da.check_ship_model("other", ship, required=False)
    with pytest.raises(SystemExit):
        da.check_ship_model("other", ship, required=True)


START = datetime(2026, 10, 1, tzinfo=UTC)


def test_as_of_status_uses_domain_dormancy_days() -> None:
    assert (
        ds.as_of_status(
            kind="event",
            domain="cyber_incident",
            last_evidence_iso="2026-09-16T00:00:00+00:00",
            start=START,
        )
        == "dormant"
    )
    assert (
        ds.as_of_status(
            kind="event",
            domain="geopolitical",
            last_evidence_iso="2026-09-16T00:00:00+00:00",
            start=START,
        )
        == "active"
    )


def test_standing_never_dormant_and_bad_time_is_active() -> None:
    old = "2026-01-01T00:00:00+00:00"
    assert (
        ds.as_of_status(kind="standing", domain="x", last_evidence_iso=old, start=START) == "active"
    )
    assert ds.as_of_status(kind="event", domain="x", last_evidence_iso="?", start=START) == "active"


def test_as_of_title_prefers_revision_claim() -> None:
    assert ds.as_of_title("今の題名", SimpleNamespace(claim=" 当時の claim ")) == "当時の claim"
    assert ds.as_of_title("今の題名", SimpleNamespace(claim="")) == "今の題名"
    assert ds.as_of_title("今の題名", None) == "今の題名"


def test_open_at_ignores_current_status() -> None:
    start = START.isoformat()
    rows = [
        SimpleNamespace(opened_at="2026-09-01T00:00:00+00:00", closed_at=None),
        SimpleNamespace(
            opened_at="2026-09-01T00:00:00+00:00", closed_at="2026-10-05T00:00:00+00:00"
        ),
        SimpleNamespace(
            opened_at="2026-09-01T00:00:00+00:00", closed_at="2026-09-20T00:00:00+00:00"
        ),
        SimpleNamespace(opened_at="2026-10-02T00:00:00+00:00", closed_at=None),
    ]
    assert ds.open_at(rows, start) == rows[:2]


def _stats(**kw: Any) -> dict[str, Any]:
    base = {
        "candidates": 10,
        "situations_as_of": 20,
        "active_as_of": 15,
        "as_of_no_revision": 0,
        "detect_input": 100,
        "missing_kinds": 0,
    }
    return {**base, **kw}


def test_strict_eligibility_adds_as_of_and_kind_rules() -> None:
    assert ds.eligibility(_stats(), strict=True) is None
    assert ds.eligibility(_stats(active_as_of=0), strict=True) == "no_active_situations"
    assert ds.eligibility(_stats(as_of_no_revision=3), strict=True) == "as_of_title_unrecoverable"
    assert ds.eligibility(_stats(as_of_no_revision=2), strict=True) is None
    assert ds.eligibility(_stats(missing_kinds=26), strict=True) == "missing_kinds"
    assert ds.eligibility(_stats(missing_kinds=26), strict=False) is None


def test_ladder_matches_prereg_table() -> None:
    assert [(s.name, s.value_freq_cap, s.cos_threshold) for s in cc.LADDER] == [
        ("L0", None, 0.80),
        ("L1", 50, 0.80),
        ("L2", 20, 0.80),
        ("L3", 20, 0.85),
        ("L4", 20, 0.90),
    ]
    assert all(s.per_article_cap == 3 for s in cc.LADDER)


def test_keys_for_step_drops_hub_values_and_wide_articles() -> None:
    vals = {
        "a": frozenset({"hub", "rare"}),
        "b": frozenset({"hub"}),
        "c": frozenset({"x1", "x2", "x3", "x4"}),
    }
    freq = {"hub": 60, "rare": 2, "x1": 1, "x2": 1, "x3": 1, "x4": 1}
    assert cc.keys_for_step(vals, freq, cc.LADDER[0]) == {
        "a": frozenset({"hub", "rare"}),
        "b": frozenset({"hub"}),
    }
    assert cc.keys_for_step(vals, freq, cc.LADDER[1]) == {"a": frozenset({"rare"})}
    roots = cc.keys_for_step(vals, freq, cc.LADDER[1], per_article=False)
    assert roots["c"] == frozenset({"x1", "x2", "x3", "x4"})


def test_value_frequencies_counts_articles_once() -> None:
    assert cc.value_frequencies({"a": ["v", "v"], "b": ["v"]}) == {"v": 2}


def test_choose_step_takes_first_below_cap_or_none() -> None:
    s0, s1, s2 = cc.LADDER[:3]
    assert cc.choose_step([(s0, 0.308), (s1, 0.21), (s2, 0.1)]) == s1
    assert cc.choose_step([(s0, 0.30)]) is None


def test_contamination_is_by_component_and_outside_universe_is_dirty() -> None:
    comp = {"t": "t", "w1": "t", "w2": "w2"}
    dirty = cc.contaminated_articles(comp, ["t"], ["w1", "w2"])
    assert dirty == {"w1"}
    universe = {"w1", "w2"}
    assert cc.unit_contaminated(["w1"], dirty, universe)
    assert not cc.unit_contaminated(["w2"], dirty, universe)
    assert cc.unit_contaminated(["zz"], dirty, universe)
    assert cc.share_of(dirty, ["w1", "w2"]) == 0.5
    assert cc.share_of(dirty, []) is None


def test_id_tokens_keeps_stripped_variant() -> None:
    assert {"rss:abc.", "rss:abc"} <= cc.id_tokens('x "rss:abc." y')
