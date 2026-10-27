"""scripts/import_severity_axes_jsonl.py の読み込み (モデルの絞り込み・検証・重複)。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.import_severity_axes_jsonl import load_rows

_AXES = {
    "scope": "single_org",
    "impact": "data_exposure",
    "confirmation": "possible",
    "magnitude": "unknown",
    "exploitation": "not_applicable",
    "actor": "unknown",
    "target": "general_org",
}


def _write(path: Path, recs: list[dict[str, Any]]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    return path


def test_keeps_only_rows_of_the_given_model(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "a.jsonl",
        [
            {"article_id": "a1", "model": "new", "axes": _AXES},
            {"article_id": "a2", "model": "old", "axes": _AXES},
        ],
    )
    rows, other, invalid = load_rows(p, "new")
    assert [aid for aid, _ in rows] == ["a1"]
    assert (other, invalid) == (1, 0)


def test_out_of_vocabulary_axes_are_skipped(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "a.jsonl",
        [{"article_id": "a1", "model": "new", "axes": {**_AXES, "scope": "galaxy"}}],
    )
    rows, _, invalid = load_rows(p, "new")
    assert rows == [] and invalid == 1


def test_later_row_wins_for_the_same_article(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "a.jsonl",
        [
            {"article_id": "a1", "model": "new", "axes": _AXES},
            {"article_id": "a1", "model": "new", "axes": {**_AXES, "scope": "national"}},
        ],
    )
    rows, _, _ = load_rows(p, "new")
    assert len(rows) == 1 and rows[0][1]["scope"] == "national"
