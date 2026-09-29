"""s22 の学習データ: 要約の ATT&CK 引用欄と、書き直した教師の差し替え (2026-09-29)。"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from scripts.cloud_train.build_dataset_v2 import (
    _with_completion,
    fix_example,
    load_summary_corrected,
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
