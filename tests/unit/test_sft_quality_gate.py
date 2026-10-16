"""scripts/sft_quality_gate.py の単体テスト (合成した小さな学習データで各関門を検証する)。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import sft_quality_gate as gate  # noqa: E402

SUMMARY_BODY = "Attackers exploited CVE-2026-1111 at Example Corp. See https://example.test/a"


def _row(
    user: str, out: str, *, system: str | None = None, meta: dict[str, Any] | None = None
) -> dict[str, Any]:
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": user},
        {"role": "assistant", "content": out},
    ]
    row: dict[str, Any] = {"messages": messages}
    if meta:
        row["meta"] = meta
    return row


def _summary_out(**over: Any) -> str:
    base: dict[str, Any] = {
        "title_ja": "題", "importance": "low", "category": "incident", "summary": "要約です。",
        "iocs": [], "mitre_techniques": [], "mitre_evidence": [],
        "malware_families": [], "tools": [],
        "analyst_note": None, "victim_sector": None, "victim_country": None, "victim_city": None,
        "involved_countries": [], "victim_orgs": [], "remediation": None,
    }  # fmt: skip
    return json.dumps({**base, **over}, ensure_ascii=False)


def _summary_user(body: str = SUMMARY_BODY) -> str:
    head = "[task: summary]\n【記事タイトル】\nT\n【記事 ソース】\nS\n【記事 本文】\n"
    return f"{head}{body}\n\n# 出力スキーマ\nx"


def _write(
    tmp_path: Path, train: list[dict[str, Any]], valid: list[dict[str, Any]] | None = None
) -> Path:
    for name, rows in (("train", train), ("valid", valid or [])):
        (tmp_path / f"{name}.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        )
    return tmp_path


def _rows(
    tmp_path: Path, train: list[dict[str, Any]], valid: list[dict[str, Any]] | None = None
) -> list[gate.Row]:
    return gate.load_rows(_write(tmp_path, train, valid))


def test_task_mark_found_in_system_message_and_flagged_as_outside_user(tmp_path: Path) -> None:
    rows = _rows(tmp_path, [_row("見出し: a", '{"kind": "other"}', system="[task: kind]\n")])
    res = gate.check_task_marks(rows, {})
    assert rows[0].task == "kind"
    assert res.detail["mark_not_in_user_message"] == {"kind": 1}


def test_unmarked_row_must_be_knowledge_qa(tmp_path: Path) -> None:
    rows = _rows(
        tmp_path,
        [
            _row("Explain the MITRE ATT&CK technique T1027.", "An adversary may."),
            _row("hello world", '{"a": 1}'),
        ],
    )
    res = gate.check_task_marks(rows, {})
    assert res.detail["unmarked_not_qa"] == 1


def test_manifest_count_mismatch_is_a_violation(tmp_path: Path) -> None:
    rows = _rows(tmp_path, [_row(_summary_user(), _summary_out())])
    ok = gate.check_task_marks(rows, {"task_counts": {"summary": {"new_train": 1, "new_valid": 0}}})
    bad = gate.check_task_marks(
        rows, {"task_counts": {"summary": {"new_train": 2, "new_valid": 0}}}
    )
    assert ok.violations == 0
    assert bad.violations == 1


def test_summary_extra_key_even_when_empty_is_rejected(tmp_path: Path) -> None:
    out = json.loads(_summary_out())
    out["bluf"] = ""
    rows = _rows(tmp_path, [_row(_summary_user(), json.dumps(out))])
    res = gate.check_schema(rows)
    assert res.violations == 1
    assert res.detail["failing_rows_reasons"]["summary"]["extra_key:bluf"] == 1


def test_valid_summary_passes_schema(tmp_path: Path) -> None:
    assert (
        gate.check_schema(_rows(tmp_path, [_row(_summary_user(), _summary_out())])).violations == 0
    )


def test_out_of_range_enum_is_rejected(tmp_path: Path) -> None:
    rows = _rows(tmp_path, [_row("[task: kind]\nx", '{"kind": "weird"}')])
    assert gate.check_schema(rows).violations == 1


def test_free_text_task_wrapped_in_json_is_rejected(tmp_path: Path) -> None:
    rows = _rows(
        tmp_path,
        [
            _row("[task: pir_focus]\nx", '{"point": "a"}'),
            _row("[task: pir_focus]\ny", "本文のみ。"),
        ],
    )
    assert gate.check_schema(rows).violations == 1


def test_provenance_missing_counts_every_row_and_multiple_versions_fail(tmp_path: Path) -> None:
    meta = {"teacher": "t", "effort": "medium", "rubric": "v1", "input_sha": "x"}
    rows = _rows(
        tmp_path,
        [
            _row(_summary_user(), _summary_out(), meta=meta),
            _row(_summary_user("b"), _summary_out()),
        ],
    )
    assert gate.check_provenance(rows, {}).violations == 1
    rows = _rows(
        tmp_path,
        [
            _row(_summary_user(), _summary_out(), meta=meta),
            _row(_summary_user("b"), _summary_out(), meta={**meta, "rubric": "v2"}),
        ],
    )
    assert gate.check_provenance(rows, {}).violations == 1  # 版が 2 種類


def test_provenance_can_come_from_manifest(tmp_path: Path) -> None:
    rows = _rows(tmp_path, [_row(_summary_user(), _summary_out(), meta={"input_sha": "x"})])
    manifest = {"provenance": {"summary": {"teacher": "t", "effort": "m", "rubric": "r"}}}
    assert gate.check_provenance(rows, manifest).violations == 0


def test_input_sha_is_uninspectable_without_teacher_sha(tmp_path: Path) -> None:
    res = gate.check_input_sha(_rows(tmp_path, [_row(_summary_user(), _summary_out())]))
    assert res.violations == 0
    assert res.uninspectable == {"summary": 1}


def test_input_sha_mismatch_is_a_violation(tmp_path: Path) -> None:
    meta = {"input_sha": "aaa", "teacher_input_sha": "bbb"}
    assert (
        gate.check_input_sha(
            _rows(tmp_path, [_row(_summary_user(), _summary_out(), meta=meta)])
        ).violations
        == 1
    )


def test_token_limit_uses_character_approximation_and_says_so(tmp_path: Path) -> None:
    rows = _rows(tmp_path, [_row(_summary_user("x" * 200), _summary_out())])
    counter = gate.TokenCounter(tmp_path / "no_tokenizer_here")
    res = gate.check_token_limits(rows, gate.count_tokens(rows, counter), counter, {"*": 100})
    assert counter.approximate is True
    assert res.detail["approximate"] is True
    assert res.violations == 1


def test_grounding_flags_cve_hash_url_not_in_body(tmp_path: Path) -> None:
    out = _summary_out(
        summary="CVE-2026-1111 と CVE-2026-9999 https://example.test/a https://evil.test/z",
        iocs=["d41d8cd98f00b204e9800998ecf8427e"],
    )
    res = gate.check_grounding(_rows(tmp_path, [_row(_summary_user(), out)]))
    assert res.violations == 1
    assert res.detail["items_not_in_body"] == {"cve": 1, "hash": 1, "url": 1}


def test_grounding_passes_when_everything_is_in_body(tmp_path: Path) -> None:
    out = _summary_out(summary="CVE-2026-1111 https://example.test/a")
    assert gate.check_grounding(_rows(tmp_path, [_row(_summary_user(), out)])).violations == 0


def test_name_mismatch_is_informational_not_a_violation(tmp_path: Path) -> None:
    out = _summary_out(victim_orgs=["Ghost Inc"])
    res = gate.check_grounding(_rows(tmp_path, [_row(_summary_user(), out)]))
    assert res.violations == 0
    assert res.detail["name_mismatches_info_only"] == 1


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"summary": "あ" * 2001}, "summary_over_2000"),
        ({"remediation": "あ" * 1001}, "remediation_over_1000"),
        ({"mitre_techniques": [f"T{1000 + i}" for i in range(20)]}, "ttp_ge_20"),
        (
            {"mitre_evidence": [{"technique": "T1", "quote": "not in the body"}]},
            "mitre_quote_not_verbatim",
        ),
    ],
)
def test_runaway_conditions(tmp_path: Path, over: dict[str, Any], reason: str) -> None:
    res = gate.check_runaway(_rows(tmp_path, [_row(_summary_user(), _summary_out(**over))]))
    assert res.detail["reasons"].get(reason) == 1


def test_verbatim_quote_with_different_whitespace_passes(tmp_path: Path) -> None:
    ev = [{"technique": "T1", "quote": "exploited   CVE-2026-1111"}]
    assert (
        gate.check_runaway(
            _rows(tmp_path, [_row(_summary_user(), _summary_out(mitre_evidence=ev))])
        ).violations
        == 0
    )


def test_length_ratio_over_baseline_fails_and_missing_baseline_fails() -> None:
    profile = {"summary": {"p50": 130.0, "p90": 100.0, "p99": 100.0}}
    base = {"summary": {"p50": 100.0, "p90": 100.0, "p99": 100.0}}
    assert gate.check_length_vs_baseline(profile, base).violations == 1
    assert gate.check_length_vs_baseline(profile, None).violations == 1


def test_exact_duplicates_conflicts_and_overlap(tmp_path: Path) -> None:
    a = _row("[task: kind]\nx", '{"kind": "other"}')
    b = _row("[task: kind]\nx", '{"kind": "stats"}')
    rows = _rows(tmp_path, [a, a, b], valid=[_row("[task: kind]\nx", '{"kind": "other"}')])
    res = gate.check_duplicates(rows, {})
    assert res.detail["same_input_different_label_groups"] == 1
    assert res.detail["train_valid_overlap_inputs"] == 1
    assert (
        res.detail["exact_duplicate_extra_rows_by_task"]["kind"] == 2
    )  # train の 2 行 + valid の 1 行 = 3 本 → 余り 2


def test_intended_exposure_allows_repeats(tmp_path: Path) -> None:
    a = _row("[task: detect]\nx", '{"open": [], "rejected": []}')
    res = gate.check_duplicates(_rows(tmp_path, [a, a, a]), {"intended_exposures": {"detect": 3}})
    assert res.violations == 0


def test_guard_hits_and_detect_date(tmp_path: Path) -> None:
    long_title = "評価に使った記事の長い見出し"
    rows = _rows(
        tmp_path,
        [
            _row(f"[task: kind]\n見出し: {long_title}", '{"kind": "other"}'),
            _row(
                "[task: detect]\n期間 (2026-09-12 00:00 〜 2026-09-12 23:59 JST)",
                '{"open": [], "rejected": []}',
            ),
        ],
    )
    res = gate.check_guard(rows, "2026-09-10", guard=(set(), {long_title}))
    assert res.detail["guard_hits_by_task"] == {"kind": 1}
    assert res.detail["rows_newer_than_eval_oldest"] == 1
    assert res.violations == 2


def test_unreadable_guard_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> tuple[set[str], set[str]]:
        raise FileNotFoundError("x")

    monkeypatch.setattr(gate, "_load_guard", boom)
    assert (
        gate.check_guard(
            _rows(tmp_path, [_row("[task: kind]\nx", '{"kind": "other"}')]), "2026-09-10"
        ).violations
        == 1
    )


def test_run_gate_end_to_end_fails_and_cli_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate, "_load_guard", lambda: (set(), set()))
    ds = tmp_path / "ds"
    ds.mkdir()
    _write(ds, [_row(_summary_user(), _summary_out(bluf="x"))])
    report = gate.run_gate(ds, None, None, tokenizer_dir=tmp_path / "none")
    assert report["passed"] is False
    assert report["checks"][0]["name"] == "1_task_marks"
    code = gate.main(
        [str(ds), "--baseline", str(tmp_path / "nobase"), "--tokenizer-dir", str(tmp_path / "none")]
    )
    assert code == 1
    assert (ds / "quality_gate.json").exists()
    assert "FAIL" in gate.format_table(report)


def _triage_user(summary: str) -> str:
    return f"[task: triage]\nフィード: F\nタイトル: T\n概要: {summary}\n\n## 判定基準 (high)\nx"


def test_empty_triage_summary_is_a_violation(tmp_path: Path) -> None:
    rows = _rows(
        tmp_path,
        [
            _row(_triage_user(""), '{"importance": "low", "reason": "r"}'),
            _row(_triage_user("本文の冒頭"), '{"importance": "low", "reason": "r"}'),
        ],
    )
    res = gate.check_empty_input_fields(rows)
    assert res.violations == 1
    assert res.detail == {"triage:概要": 1}


def test_empty_summary_body_is_a_violation_and_filled_passes(tmp_path: Path) -> None:
    res = gate.check_empty_input_fields(
        _rows(
            tmp_path,
            [_row(_summary_user(""), _summary_out()), _row(_summary_user(), _summary_out())],
        )
    )
    assert res.violations == 1
