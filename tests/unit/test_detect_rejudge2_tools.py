"""DETECT-REJUDGE-2 (docs/replay_prereg/detect_rejudge_v2.md) の道具の純粋部分。"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS / "gpu_window"))
sys.path.insert(0, str(_SCRIPTS))

import detect_common as dc  # noqa: E402

V2 = dc.profile_named("rejudge2")
ON = dict.fromkeys(dc.ARM_FLAG_KEYS, "1")
OFF = dict.fromkeys(dc.ARM_FLAG_KEYS, "0")


def test_v2_profile_matches_prereg() -> None:
    assert V2.prereg_name == "detect_rejudge_v2"
    assert V2.seed == 20261110
    assert V2.range_start == datetime(2026, 11, 4, 12, tzinfo=UTC)
    assert V2.range_end == datetime(2026, 11, 10, 6, tzinfo=UTC)
    assert len(dc.build_bins(V2.range_start, V2.range_end)) == 23
    assert V2.max_per_day == 4
    assert V2.as_of_strict and V2.check_ship_resolution and V2.freeze_org_alias
    for f in (
        "src/assessment/claim_fidelity.py",
        "src/assessment/claim_salvage.py",
        "src/assessment/detect_merge_gate.py",
        "src/assessment/detect_recall_flags.py",
    ):
        assert f in V2.code_files


def test_v2_range_after_prior_windows_with_gap() -> None:
    prior = dc.profile_named("decompose")
    assert (V2.range_start - prior.range_end).total_seconds() == 12 * 3600
    assert dc.profile_named("rejudge").range_end < prior.range_start


def test_v2_arms_fix_recall_flags() -> None:
    assert dc.fixed_flags(V2) == {"current": OFF, "ship": ON}
    assert dc.resolve_arm_flags("", V2) == {"current": OFF, "ship": ON}
    raw = ",".join(f"ship:{k}=1" for k in dc.ARM_FLAG_KEYS)
    raw += "," + ",".join(f"current:{k}=0" for k in dc.ARM_FLAG_KEYS)
    assert dc.resolve_arm_flags(raw, V2) == {"current": OFF, "ship": ON}


def test_v2_rejects_flags_that_differ_from_prereg() -> None:
    with pytest.raises(SystemExit):
        dc.resolve_arm_flags("ship:DETECT_CLAIM_SALVAGE=1", V2)


def test_other_profiles_keep_env_driven_flags() -> None:
    rej = dc.profile_named("rejudge")
    assert dc.fixed_flags(rej) == {}
    assert dc.resolve_arm_flags("", rej) == {}
    assert dc.resolve_arm_flags("ship:DETECT_CLAIM_SALVAGE=1", rej) == {
        "ship": {"DETECT_CLAIM_SALVAGE": "1"}
    }


def test_frozen_repo_org_alias() -> None:
    window = dc.with_org_alias({"articles": {}}, {"pairs": [["a", "b"]], "dict": {"x": "y"}})
    repo = dc.FrozenRepo(window)
    assert repo.list_org_identity_same_pairs(form="f", min_logit=0.0) == [("a", "b")]
    assert repo.load_org_alias_dict() == {"x": "y"}


def test_frozen_repo_without_alias_fails_like_before() -> None:
    base: dict[str, object] = {"articles": {}}
    assert dc.with_org_alias(base, None) == base
    repo = dc.FrozenRepo(dc.with_org_alias(base, None))
    with pytest.raises(RuntimeError):
        repo.load_org_alias_dict()


def test_with_org_alias_does_not_mutate() -> None:
    base: dict[str, object] = {"articles": {}}
    out = dc.with_org_alias(base, {"pairs": [], "dict": {}})
    assert dc.ORG_ALIAS_KEY not in base and dc.ORG_ALIAS_KEY in out


def test_sample_windows_respects_four_per_day() -> None:
    bins = dc.build_bins(V2.range_start, V2.range_end)
    picked = dc.sample_windows(bins, seed=V2.seed, n=20, max_per_day=4)
    per_day: dict[str, int] = {}
    for b in picked:
        per_day[b.day] = per_day.get(b.day, 0) + 1
    assert max(per_day.values()) <= 4 and len(picked) == 20
