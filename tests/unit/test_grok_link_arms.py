"""scripts/gpu_window/grok_link_arms.py の純粋部分
(標本の抽出・腕・無効の判定)。LLM・DB は使わない。"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS / "gpu_window"))

import grok_link_arms as arms  # noqa: E402
import grok_link_sample as gls  # noqa: E402

STORED = datetime(2026, 11, 6, 12, tzinfo=UTC)


def _sid(dt: datetime) -> str:
    return str((int(dt.timestamp() * 1000) - gls.X_EPOCH_MS) << 22)


def _page(url: str, text: str, ok: bool = True, skip: str | None = None) -> dict[str, object]:
    return {"url": url, "final_url": url, "ok": ok, "text": text, "skip_reason": skip}


def test_posted_at_roundtrips_status_id() -> None:
    dt = datetime(2026, 10, 30, 3, 4, 5, tzinfo=UTC)
    assert abs((gls.posted_at(_sid(dt)) - dt).total_seconds()) < 0.01


def test_draw_is_seeded_and_order_independent() -> None:
    cands = [gls.Candidate(str(1000 + i), f"a{i}", False) for i in range(75)]
    a = gls.draw(cands)
    b = gls.draw(list(reversed(cands)))
    assert [c.status_id for c in a] == [c.status_id for c in b]
    assert len({c.status_id for c in a}) == 40
    assert [c.status_id for c in a] != [c.status_id for c in gls.draw(cands, seed=1)]


def test_draw_refuses_below_forty() -> None:
    with pytest.raises(gls.NotEnoughCandidatesError):
        gls.draw([gls.Candidate(str(i), f"a{i}", False) for i in range(39)])


def test_draw_refused_before_window_closes() -> None:
    early = datetime(2026, 11, 6, 23, 0, tzinfo=gls.JST)
    with pytest.raises(gls.DrawTooEarlyError):
        gls.assert_draw_allowed(early, allow_early=False)
    gls.assert_draw_allowed(early, allow_early=True)
    gls.assert_draw_allowed(datetime(2026, 11, 7, 0, 0, tzinfo=gls.JST), allow_early=False)


def test_population_sql_rejects_non_digit_ids() -> None:
    assert "('123')" in gls.population_sql(["123"])
    with pytest.raises(ValueError):
        gls.population_sql(["1; DROP TABLE x"])


def _row(i: int, sid: str, created: datetime, gate: bool = True) -> dict[str, object]:
    return {
        "id": i,
        "article_id": f"a{i}",
        "url": f"https://x.com/u/status/{sid}",
        "created_at": created,
        "gate_ok": gate,
        "any_ok": True,
    }


def test_stage_counts_fold_window_and_gate() -> None:
    inside = STORED - timedelta(days=2)
    s1 = _sid(inside)
    s2 = _sid(inside + timedelta(hours=1))
    s3 = _sid(datetime(2026, 10, 1, tzinfo=UTC))
    rows = [
        _row(1, s1, STORED),
        _row(2, s1, STORED + timedelta(hours=1)),
        _row(3, s2, STORED, gate=False),
        _row(4, s3, STORED),
        {**_row(5, "0", STORED), "url": "https://x.com/u"},
    ]
    c = gls.stage_counts(rows, excluded=[])
    assert c["folded_duplicate_rows"] == 1 and c["no_status_id"] == 1
    assert c["posts_outside_window"] == 1 and c["posts_no_gate_passing_page"] == 1
    assert c["candidates_before_r18"] == 1
    assert gls.stage_counts(rows, excluded=[s1])["posts_in_gold44"] == 1


def test_r18_removes_gold_component_and_hub_keys() -> None:
    comp = {"g1": "c1", "a": "c1", "b": "c2"}
    cands = [gls.Candidate("1", "a", False), gls.Candidate("2", "b", False)]
    kept, removed = gls.r18_filter(cands, ["g1"], comp)
    assert [c.article_id for c in kept] == ["b"]
    assert [c.article_id for c in removed] == ["a"]
    hub = gls.limit_hub_keys({"x": ["CVE-1", "CVE-2", "CVE-3", "CVE-4"], "y": ["CVE-1"]})
    assert "x" not in hub and hub["y"] == frozenset({"CVE-1"})


def test_plan_units_orders_runs_and_swaps_arms() -> None:
    ids = [str(i) for i in range(40)]
    units = arms.plan_units(ids)
    assert len(units) == 160
    assert {u[1] for u in units[:80]} == {1} and {u[1] for u in units[80:]} == {2}
    assert arms.arm_order("7", 1) == arms.arm_order("7", 1)
    assert len({arms.arm_order(s, 1) for s in ids}) == 2


def test_prompt_bodies_with_arm_adds_only_usable_pages() -> None:
    lc = json.dumps(
        [
            _page("https://e.example/a", "REFTEXT"),
            _page("https://e.example/b", "OFF", skip="off_topic"),
        ]
    )
    out = arms.prompt_bodies("POST", lc)
    assert out["without"] == "POST"
    assert out["with"].startswith("POST") and "REFTEXT" in out["with"] and "OFF" not in out["with"]
    only_off = json.dumps([_page("https://e.example/b", "OFF", skip="off_topic")])
    assert arms.prompt_bodies("POST", only_off)["with"] == "POST"


def test_pages_from_linked_context_ignores_unknown_keys() -> None:
    pages = arms.pages_from_linked_context(json.dumps([{**_page("u", "t"), "zzz": 1}]))
    assert pages[0].text == "t"


def test_reconstruct_gate_post() -> None:
    assert arms.reconstruct_gate_post("TEXT\n[引用] Q\n参考 URL: https://a") == "TEXT\nQ"
    assert arms.reconstruct_gate_post("TEXT\n参考 URL: https://a") == "TEXT\n"
    assert arms.reconstruct_gate_post("TEXT") == "TEXT\n"


def test_reconstruct_gate_post_with_empty_post_text() -> None:
    # 投稿の本文が空: body は "[引用] Q" で始まる。関門へ渡した "\nQ" に戻る
    assert arms.reconstruct_gate_post("[引用] Q") == "\nQ"
    assert arms.reconstruct_gate_post("[引用] Q\n参考 URL: https://a") == "\nQ"


def test_gate_mismatch_flags_stored_skip_the_gate_would_not_set() -> None:
    # 取得失敗のページは関門の対象外 -> 当て直しは skip なし。保存が off_topic なら食い違い
    stale = _page("https://e.example/a", "", ok=False, skip="off_topic")
    clean = _page("https://e.example/a", "", ok=False, skip=None)
    row = {"status_id": "9", "body": "post", "linked_context": json.dumps([stale])}
    assert arms.gate_mismatches([row]) == ["9"]
    assert arms.gate_mismatches([{**row, "linked_context": json.dumps([clean])}]) == []


def test_generation_invalidations() -> None:
    ids = [str(i) for i in range(40)]
    ok = [{"status_id": s, "run": r, "ok": True} for s in ids for r in (1, 2)]
    assert arms.generation_invalidations(ok, ids) == []
    nine = [dict(x, ok=False) if i < 9 else x for i, x in enumerate(ok)]
    assert any("missing_summaries" in r for r in arms.generation_invalidations(nine, ids))
    both = [dict(x, ok=False) if x["status_id"] in {"0", "1", "2", "3", "4"} else x for x in ok]
    assert any("both_runs" in r for r in arms.generation_invalidations(both, ids))


def test_model_and_stability_invalidations() -> None:
    good = {arms.EXPECTED_MODEL: arms.EXPECTED_DIGEST}
    assert arms.model_invalidations(arms.EXPECTED_MODEL, good, good) == []
    bad = {arms.EXPECTED_MODEL: "deadbeef"}
    assert len(arms.model_invalidations("other", good, bad)) == 2
    assert arms.stability_invalidations({"template_sha256": "a"}, {"template_sha256": "a"}) == []
    assert arms.stability_invalidations({"generation_files": "a"}, {"generation_files": "b"}) == [
        "generation_files_changed"
    ]


def test_check_attestation_requires_matching_tools(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        arms.check_attestation(tmp_path / "none.json")
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"tools": {"x": "y"}}))
    with pytest.raises(SystemExit):
        arms.check_attestation(p)
    p.write_text(json.dumps({"tools": arms.tool_shas(), "generation_files": {"x": "y"}}))
    with pytest.raises(SystemExit):
        arms.check_attestation(p)
    good = {"tools": arms.tool_shas(), "generation_files": arms.generation_shas()}
    p.write_text(json.dumps({**good, "head_sha": "h"}))
    assert arms.check_attestation(p)["head_sha"] == "h"


def test_dry_run_prints_plan(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    import argparse

    assert arms.dry_run(argparse.Namespace(out_dir=str(tmp_path))) == 0
    out = capsys.readouterr().out
    assert "160 要約" in out and arms.EXPECTED_MODEL in out


def test_generation_path_files_exist_and_are_hashed() -> None:
    shas = arms.generation_shas()
    assert set(shas) == set(arms.GENERATION_PATH)
    assert all(len(v) == 64 for v in shas.values())  # 無いファイルは空文字になる
    assert {"src/grok/link_fetch.py", "prompts/briefing/summarizer.j2"} <= set(shas)


def test_generation_shas_missing_file_is_empty(tmp_path: Path) -> None:
    assert set(arms.generation_shas(tmp_path).values()) == {""}


def test_generation_path_changes_detects_diff(tmp_path: Path) -> None:
    import subprocess

    def git(*a: str) -> str:
        r = subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *a],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
        return r.stdout.strip()

    git("init", "-q")
    for rel in arms.GENERATION_PATH:
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("v1")
    git("add", "-A")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (tmp_path / "src/grok/link_relevance.py").write_text("v2")
    git("commit", "-qam", "change")
    orig = arms.GENERATION_BASE
    arms.GENERATION_BASE = base
    try:
        assert arms.generation_path_changes(tmp_path) == ["src/grok/link_relevance.py"]
    finally:
        arms.GENERATION_BASE = orig


def test_check_attestation_refuses_changed_generation_file(tmp_path: Path) -> None:
    shas = dict(arms.generation_shas())
    shas["src/grok/link_fetch.py"] = "0" * 64
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"tools": arms.tool_shas(), "generation_files": shas}))
    with pytest.raises(SystemExit):
        arms.check_attestation(p)


def test_run_step_disables_local_usage_before_anything_else(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import argparse

    monkeypatch.setenv("LLM_USAGE_LOCAL", "1")
    args = argparse.Namespace(
        attestation=str(tmp_path / "none.json"), out_dir=str(tmp_path), allow_early_for_test=False
    )
    with pytest.raises(SystemExit):  # 証明書が無いので LLM に到達する前に止まる
        arms.run_step(args)
    assert os.environ["LLM_USAGE_LOCAL"] == "0"


def test_probe_article_carries_feed_title_and_template_renders_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = arms.probe_article("1", feed_title="Some Author (@handle)")
    assert a.feed_title == "Some Author (@handle)"
    assert arms.probe_article("1").feed_title == "probe"  # 指紋用の固定値は不変
    from src.pipeline.briefing import _prompt_article
    from src.pipeline.dispatch import _load_template

    # DB を使わず legacy .j2 に落とす
    monkeypatch.setattr("src.prompts.rubric_store.build_summarizer_template", lambda _path: None)
    tpl = _load_template()
    out = tpl.render(article=_prompt_article(a), body="BODY")
    assert "Some Author (@handle)" in out


def test_fetch_post_rows_selects_feed_title_and_carries_it() -> None:
    class Conn:
        sql = ""

        def execute(self, sql: str, params: tuple[str, ...]) -> Conn:
            self.sql = sql
            return self

        def fetchall(self) -> list[dict[str, str]]:
            return [
                {
                    "status_id": "5",
                    "article_id": "a5",
                    "url": "u",
                    "body": "b",
                    "linked_context": "[]",
                    "feed_title": "Author (@h)",
                }
            ]

    conn = Conn()
    rows = arms.fetch_post_rows(conn, [{"status_id": "5", "article_id": "a5"}])
    assert "feed_title" in conn.sql and rows[0]["feed_title"] == "Author (@h)"


def test_verify_gold44_rejects_changed_file(tmp_path: Path) -> None:
    p = tmp_path / "sample.json"
    p.write_text("[]")
    with pytest.raises(SystemExit):
        arms.verify_gold44(p)


def test_draw_output_path_separates_early_test(tmp_path: Path) -> None:
    out = tmp_path / "grok_link_judge_1107"
    assert arms.draw_output_path(out, False) == out / "sample.json"
    early = arms.draw_output_path(out, True)
    assert early.parent != out and early.name == "sample.json"


def test_draw_early_test_refuses_existing_early_sample(tmp_path: Path) -> None:
    import argparse

    out = tmp_path / "judge"
    early = arms.draw_output_path(out, True)
    early.parent.mkdir(parents=True)
    early.write_text("{}")
    args = argparse.Namespace(
        out_dir=str(out), attestation=str(tmp_path / "x.json"), allow_early_for_test=True
    )
    with pytest.raises(SystemExit, match="既にある"):
        arms.draw_step(args)
