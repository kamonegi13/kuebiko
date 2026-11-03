"""scripts/gpu_window/detect_* (GPU-B) の純粋部分: 窓の抽出・腕の設定・書き込みの封・代役 repo。"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS / "gpu_window"))
sys.path.insert(0, str(_SCRIPTS))

import detect_common as dc  # noqa: E402
import detect_snapshot as ds  # noqa: E402


def test_bins_cover_prereg_range_in_32_six_hour_slots() -> None:
    bins = dc.build_bins()
    assert len(bins) == 32
    assert bins[0].start == datetime(2026, 10, 27, tzinfo=UTC)
    assert bins[-1].end == datetime(2026, 11, 4, tzinfo=UTC)
    assert all((b.end - b.start).total_seconds() == 6 * 3600 for b in bins)


def test_sample_is_deterministic_and_respects_per_day_cap() -> None:
    bins = dc.build_bins()
    first = dc.sample_windows(bins)
    second = dc.sample_windows(list(reversed(bins)))  # 入力順に依らない (昇順に並べてから shuffle)
    assert [b.key for b in first] == [b.key for b in second]
    assert len(first) == dc.N_WINDOWS
    per_day: dict[str, int] = {}
    for b in first:
        per_day[b.day] = per_day.get(b.day, 0) + 1
    assert max(per_day.values()) <= dc.MAX_PER_DAY


def test_sample_uses_all_when_fewer_than_n() -> None:
    few = dc.build_bins()[::4][:5]  # 日をまたぐ 5 区間 (日あたり上限に掛からない)
    assert len(dc.sample_windows(few)) == 5


def test_arm_order_is_deterministic_and_covers_both_arms() -> None:
    a = dc.arm_order(3, 2)
    assert a == dc.arm_order(3, 2)
    assert {x.name for x in a} == {"current", "ship"}


def test_plan_units_has_three_runs_per_arm_per_window() -> None:
    units = dc.plan_units(["w1", "w2"])
    assert len(units) == 2 * 3 * 2
    assert sum(1 for u in units if u[0] == "w1" and u[2] == "ship") == 3


def test_arm_config_matches_prereg_table() -> None:
    assert dc.CURRENT_ARM.model == "kuebiko-sft:s23i" and dc.CURRENT_ARM.decompose_mode == "shadow"
    assert dc.SHIP_ARM.model == "kuebiko-sft:s24ci65" and dc.SHIP_ARM.decompose_mode == "1"
    assert dc.arm_env(dc.SHIP_ARM) == {"DETECT_DECOMPOSE": "1", "LLM_USAGE_LOCAL": "0"}
    assert dc.arm_env(dc.CURRENT_ARM)["DETECT_DECOMPOSE"] == "shadow"


def test_pgoptions_adds_read_only_once() -> None:
    once = dc.pgoptions_read_only("-c statement_timeout=1000")
    assert dc.READONLY_PGOPTION in once
    assert dc.pgoptions_read_only(once) == once
    assert dc.pgoptions_read_only(None) == dc.READONLY_PGOPTION


def test_write_guard_rejects_non_read_only_session() -> None:
    dc.assert_read_only_setting("on")
    with pytest.raises(dc.WriteGuardError):
        dc.assert_read_only_setting("off")


def test_detect_model_sha_matches_prereg() -> None:
    root = Path(__file__).resolve().parents[2]
    assert dc.verify_detect_model(root / dc.DETECT_MODEL_PATH) == dc.DETECT_MODEL_SHA256


def test_frozen_repo_reads_only_frozen_values() -> None:
    frozen: dict[str, Any] = {
        "articles": {
            "a1": {"summary": "s", "feed_title": "f", "created_at": "t", "body_prefix": "b"}
        },
        "event_items": {"a1": ["e1", "e2"]},
        "embeddings": {"a1": dc.encode_vec([0.5, 0.25])},
    }
    repo = dc.FrozenRepo(frozen)
    assert repo.get_articles_by_ids(["a1", "zz"])["a1"].summary == "s"
    assert repo.get_article_bodies(["a1"]) == {"a1": "b"}
    assert repo.event_item_ids_by_article(["a1"]) == {"a1": {"e1", "e2"}}
    assert list(repo.load_summary_embeddings(["a1"])["a1"]) == [0.5, 0.25]
    assert not hasattr(repo, "set_article_kind")  # 書き込み口は持たない


def test_merge_scores_from_frozen_roundtrip() -> None:
    frozen = {
        "merge": {"sids": ["s1"], "scores": [["s1", "a1", True, 0.9], ["s1", "a2", False, None]]}
    }
    scores, sids = dc.merge_scores_from_frozen(frozen)
    assert (
        sids == ["s1"]
        and scores[("s1", "a1")] == (True, 0.9)
        and scores[("s1", "a2")] == (False, None)
    )
    assert dc.merge_scores_from_frozen({"merge": None}) == ({}, [])


def test_inputs_hash_roundtrip_and_tamper(tmp_path: Path) -> None:
    hashes = dc.write_json_with_sha(tmp_path, dc.INPUTS_NAME, dc.INPUTS_SHA_NAME, {"windows": []})
    assert dc.verify_inputs(tmp_path) == hashes["inputs_sha256"]
    (tmp_path / dc.INPUTS_NAME).write_text('{"windows": [1]}', encoding="utf-8")
    with pytest.raises(SystemExit):
        dc.verify_inputs(tmp_path)


def test_eligibility_excludes_small_and_empty_windows() -> None:
    assert ds.eligibility({"candidates": 4, "situations_as_of": 9}) == "candidates_lt5"
    assert ds.eligibility({"candidates": 9, "situations_as_of": 0}) == "no_active_situations"
    assert ds.eligibility({"candidates": 5, "situations_as_of": 1}) is None
