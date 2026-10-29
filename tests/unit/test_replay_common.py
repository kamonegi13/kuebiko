"""scripts/replay の共通部品 (RP-1) のテスト。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from scripts.replay import _common as c
from scripts.replay._manifest import Manifest


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@kuebiko.example")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "docs/replay_prereg").mkdir(parents=True)
    (tmp_path / "docs/replay_prereg/README.md").write_text("r")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


# --- read-only ---------------------------------------------------------------


def test_readonly_refuses_write_statements(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    sqlite3.connect(db).executescript("CREATE TABLE a(x); INSERT INTO a VALUES (1);")
    conn = c.open_readonly_sqlite(str(db))
    for sql in ("DELETE FROM a", "UPDATE a SET x=2", "SET x=1", "SELECT 1; DROP TABLE a"):
        with pytest.raises(c.ReadOnlyViolationError):
            conn.execute(sql)
    assert conn.execute("SELECT x FROM a").fetchone()[0] == 1


def test_readonly_allows_string_literal_with_keyword(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    sqlite3.connect(db).executescript("CREATE TABLE a(x)")
    conn = c.open_readonly_sqlite(str(db))
    assert conn.execute("SELECT 'update; delete' AS v").fetchone()[0] == "update; delete"


def test_model_tiers_version_reads_max(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    sqlite3.connect(db).executescript(
        "CREATE TABLE app_config_versions(config_key, version);"
        "INSERT INTO app_config_versions VALUES ('model_tiers', 44), ('model_tiers', 45);"
    )
    assert c.model_tiers_version(c.open_readonly_sqlite(str(db))) == 45


def test_event_ts_sql_uses_ssot() -> None:
    assert "published_at" in c.event_ts_sql("x") and "x." in c.event_ts_sql("x")


# --- R18 / split ---------------------------------------------------------------


def test_resolve_roots_follows_chain_and_survives_cycle() -> None:
    roots = c.resolve_roots({"a": "b", "b": "c", "c": None, "x": "y", "y": "x"})
    assert roots["a"] == "c" and roots["x"] in {"x", "y"}


def test_components_union_root_cve_org_and_cosine() -> None:
    comp = c.build_components(
        ["a1", "a2", "a3", "a4", "a5", "a6"],
        root_of={"a1": "r1", "a2": "r1", "a3": "r3", "a4": "r4", "a5": "r5", "a6": "r6"},
        cves={"a2": frozenset({"CVE-1"}), "a3": frozenset({"CVE-1"})},
        victim_orgs={"a3": frozenset({"org"}), "a4": frozenset({"org"})},
        embeddings={"a5": [1.0, 0.0], "a6": [0.9, 0.1], "a1": [0.0, 1.0]},
    )
    assert len({comp[a] for a in ("a1", "a2", "a3", "a4")}) == 1
    assert comp["a5"] == comp["a6"] != comp["a1"]


def test_split_excludes_test_rows_sharing_a_closure_with_tune() -> None:
    ts = {
        "t1": datetime(2026, 9, 1),
        "s1": datetime(2026, 10, 5),
        "s2": datetime(2026, 10, 6),
    }
    comp = {"t1": "k", "s1": "k", "s2": "z"}

    res = c.split_by_event_time(ts, comp, datetime(2026, 10, 1))

    assert res.tune == {"t1"} and res.test == {"s2"} and res.excluded == {"s1"}
    c.assert_no_shared_component(res.tune, res.test, comp)
    assert res.n_excluded_components == 1


# --- gate ---------------------------------------------------------------------


def test_prereg_refuses_missing_untracked_and_dirty(repo: Path) -> None:
    with pytest.raises(c.GateRefusedError, match="missing"):
        c.require_prereg("x", repo)
    p = repo / "docs/replay_prereg/x.md"
    p.write_text("v1")
    with pytest.raises(c.GateRefusedError, match="not tracked"):
        c.require_prereg("x", repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "prereg")
    p.write_text("v2")
    with pytest.raises(c.GateRefusedError, match="uncommitted"):
        c.require_prereg("x", repo)


def test_prereg_records_commit_sha_and_rejects_bad_names(repo: Path) -> None:
    (repo / "docs/replay_prereg/x.md").write_text("v1")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "prereg")

    rec = c.require_prereg("x", repo)

    assert len(rec.git_sha) == 40 and rec.head_sha == rec.git_sha
    for bad in ("../x", "README", ""):
        with pytest.raises(c.GateRefusedError):
            c.require_prereg(bad, repo)


# --- judge set ----------------------------------------------------------------


def test_judge_set_refused_by_path_and_by_sha(repo: Path, tmp_path: Path) -> None:
    (repo / "data").mkdir()
    tainted = repo / "data/gold.jsonl"
    tainted.write_text('{"id":1}\n{"id":2}\n')
    copy = repo / "data/renamed.jsonl"
    copy.write_text(tainted.read_text())
    fresh = repo / "data/fresh.jsonl"
    fresh.write_text('{"id":9}\n')
    ledger = repo / "plan.yaml"
    ledger.write_text(
        "gold_sets:\n  - {id: g, tainted: true, files: [data/gold.jsonl]}\n"
        "  - {id: f, tainted: false, files: [data/fresh.jsonl]}\n"
    )
    reg = c.load_tainted_registry(ledger, repo)

    with pytest.raises(c.GateRefusedError, match="path"):
        c.identify_judge_set(tainted, reg, repo)
    with pytest.raises(c.GateRefusedError, match="sha256"):
        c.identify_judge_set(copy, reg, repo)
    ok = c.identify_judge_set(fresh, reg, repo)
    assert ok.path == "data/fresh.jsonl" and ok.rows == 1 and len(ok.sha256) == 64


# --- manifest / lock -------------------------------------------------------------


def _manifest(**over: object) -> Manifest:
    base: dict[str, object] = {
        "name": "n", "period_start": "a", "period_end": "b", "split_cutoff": "c",
        "input_kinds": {"x": "point_in_time"}, "head_sha": "h", "prereg_path": "p",
        "prereg_git_sha": "g", "prereg_blob_sha256": "s", "judge_sets": [],
        "model_tiers_version": 45, "r18_version": c.R18_VERSION,
    }  # fmt: skip
    return Manifest(**{**base, **over})  # type: ignore[arg-type]


def test_manifest_written_atomically_and_rejects_body(tmp_path: Path) -> None:
    out = c.write_manifest(_manifest(flags={"F": "1"}), tmp_path / "o")
    assert json.loads(out.read_text())["model_tiers_version"] == 45

    with pytest.raises(c.BodyLeakError):
        c.write_manifest(_manifest(judge_sets=[{"body": "x"}]), tmp_path / "o2")
    with pytest.raises(c.BodyLeakError):
        c.write_manifest(_manifest(flags={"F": "x" * 400}), tmp_path / "o3")


def test_adjudication_lock_is_exclusive(tmp_path: Path) -> None:
    path = tmp_path / "l.lock"
    with (
        c.adjudication_lock(path, owner="a"),
        pytest.raises(c.AdjudicationLockBusyError),
        c.adjudication_lock(path, owner="b"),
    ):
        pass
    with c.adjudication_lock(path, owner="c"):
        pass


# --- stats --------------------------------------------------------------------


def test_wilson_interval_known_value() -> None:
    iv = c.wilson_interval(8, 10)
    assert iv.point == 0.8 and 0.49 < iv.low < 0.50 and 0.94 < iv.high < 0.95
    assert c.wilson_interval(0, 0).high == 1.0


def test_mcnemar_exact_and_rule_of_three() -> None:
    assert c.mcnemar_exact(0, 0) == 1.0
    assert c.mcnemar_exact(5, 5) == 1.0
    assert abs(c.mcnemar_exact(0, 10) - 2 / 1024) < 1e-12
    assert c.discordant_counts([(True, False), (False, True), (True, True)]) == (1, 1)
    assert c.rule_of_three(100) == 0.03


def test_weighted_estimate_and_cluster_bootstrap() -> None:
    strata = {
        "easy": [c.Observation(f"e{i}", 1.0) for i in range(20)],
        "hard": [c.Observation(f"h{i // 2}", float(i % 2)) for i in range(20)],
    }
    weights = {"easy": 3.0, "hard": 1.0}

    assert c.weighted_estimate(strata, weights) == pytest.approx(0.875)
    iv = c.cluster_bootstrap_ci(strata, weights, seed=1, iterations=300)
    again = c.cluster_bootstrap_ci(strata, weights, seed=1, iterations=300)
    assert iv == again and iv.low <= iv.point <= iv.high
    with pytest.raises(ValueError):
        c.weighted_estimate(strata, {"easy": 1.0})
