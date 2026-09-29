"""s22 の学習データ: 要約の ATT&CK 引用欄と、書き直した教師の差し替え (2026-09-29)。"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from scripts.cloud_train.build_dataset_v2 import (
    AXES_MARKER,
    PIR_FOCUS_MARKER,
    _with_completion,
    apply_rules_fix,
    fix_example,
    load_flagged,
    load_summary_corrected,
    pir_focus_examples,
)

USER = "[task: summary]\n記事本文"


def _example(target: dict[str, Any]) -> dict[str, Any]:
    return {
        "messages": [
            {"role": "user", "content": USER},
            {"role": "assistant", "content": json.dumps(target, ensure_ascii=False)},
        ]
    }


def _key() -> str:
    return hashlib.sha256(USER.encode()).hexdigest()[:16]


def _verdict(tech: str, final: str, quote: str = "", ok: bool = True) -> dict[str, Any]:
    return {"technique": tech, "final": final, "quote": quote, "quote_ok": ok}


TTP = {
    _key(): {
        "T1566": _verdict("T1566", "明記", "フィッシングメールで侵入した"),
        "T1190": _verdict("T1190", "推測"),
        "T1059": _verdict("T1059", "文面から明らか", "", ok=False),
    }
}


def _target(example: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(example["messages"][-1]["content"])
    return out


class TestEvidence:
    def test_kept_techniques_carry_their_quote_as_last_field(self) -> None:
        example = _example({"summary": "s", "mitre_techniques": ["T1566", "T1190", "T1059"]})

        fixed = fix_example(example, Counter(), TTP, None, with_evidence=True)

        assert fixed is not None
        target = _target(fixed)
        assert target["mitre_techniques"] == ["T1566"]
        assert target["mitre_evidence"] == [
            {"technique": "T1566", "quote": "フィッシングメールで侵入した"}
        ]
        assert list(target)[-1] == "mitre_evidence"

    def test_summary_without_techniques_gets_empty_evidence(self) -> None:
        fixed = fix_example(_example({"summary": "s"}), Counter(), TTP, None, with_evidence=True)

        assert fixed is not None
        assert _target(fixed)["mitre_evidence"] == []

    def test_without_flag_the_field_is_not_added(self) -> None:
        example = _example({"summary": "s", "mitre_techniques": ["T1566"]})

        fixed = fix_example(example, Counter(), TTP, None)

        assert fixed is not None
        assert "mitre_evidence" not in _target(fixed)


class TestCorrectedSummary:
    def test_row_is_replaced_by_corrected_completion(self, tmp_path: Path) -> None:
        corrected = tmp_path / "corrected.jsonl"
        corrected.write_text(
            json.dumps({"i": 0, "completion": json.dumps({"summary": "直した"})}) + "\n",
            encoding="utf-8",
        )
        stats: Counter[str] = Counter()

        mapping = load_summary_corrected(corrected)
        row = _with_completion(_example({"summary": "元"}), mapping.get(0), stats)

        assert _target(row)["summary"] == "直した"
        assert row["messages"][0]["content"] == USER
        assert stats["要約の教師を書き直し版に差し替え"] == 1

    def test_rows_without_correction_are_untouched(self) -> None:
        original = _example({"summary": "元"})

        assert _with_completion(original, None, Counter()) is original

    def test_missing_file_means_no_corrections(self, tmp_path: Path) -> None:
        assert load_summary_corrected(tmp_path / "none.jsonl") == {}


class TestPirFocus:
    def test_examples_carry_the_production_marker_and_split_off_valid(self, tmp_path: Path) -> None:
        path = tmp_path / "focus.jsonl"
        rows = [
            {"key": f"pir_a:2026-09-{i:02d}", "prompt": "プロンプト", "completion": "要点。"}
            for i in range(1, 22)
        ] + [{"key": "pir_b:2026-09-01", "prompt": "p", "completion": "  "}]
        path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8"
        )

        train, valid = pir_focus_examples(path, Counter())

        assert len(train) + len(valid) == 21  # 空の出力は落とす
        assert len(valid) == 2
        user = train[0]["messages"][0]["content"]
        assert user == PIR_FOCUS_MARKER + "プロンプト"
        assert train[0]["messages"][1] == {"role": "assistant", "content": "要点。"}

    def test_marker_matches_production(self) -> None:
        from src.tools.model_tiers import Step
        from src.tools.task_prefix import TASK_MARKERS

        assert TASK_MARKERS[Step.PIR_DAILY_FOCUS] == PIR_FOCUS_MARKER
        assert TASK_MARKERS[Step.SEVERITY_AXES] == AXES_MARKER


def test_flagged_rows_are_those_with_issues(tmp_path: Path) -> None:
    path = tmp_path / "g.jsonl"
    path.write_text(
        json.dumps({"i": 3, "issues": [{"kind": "格上げ"}]})
        + "\n"
        + json.dumps({"i": 4, "issues": []}),
        encoding="utf-8",
    )

    assert load_flagged(path) == {3}


class TestRulesFix:
    def test_fixed_replaces_unfixable_is_dropped_clean_is_kept(self) -> None:
        clean = _example({"summary": "問題なし"})
        other_user = "[task: summary]\n別の記事"
        broken = {
            "messages": [
                {"role": "user", "content": other_user},
                {"role": "assistant", "content": json.dumps({"summary": "違反"})},
            ]
        }
        import hashlib as _h

        broken_key = _h.sha256(other_user.encode()).hexdigest()[:16]
        stats: Counter[str] = Counter()

        fixed = apply_rules_fix(
            [clean, broken], {broken_key: json.dumps({"summary": "直した"})}, stats
        )
        dropped = apply_rules_fix([clean, broken], {broken_key: None}, Counter())

        assert [_target(e)["summary"] for e in fixed] == ["問題なし", "直した"]
        assert [_target(e)["summary"] for e in dropped] == ["問題なし"]
