"""staging の付け直しを学習に読ませる入力層。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.tools.staging_inputs import (
    StagingOverlayRepo,
    axes_to_db_shape,
    load_axes_jsonl,
    load_kinds_jsonl,
    overlay_if_given,
)


class _FakeRepo:
    def get_article_kinds(self, ids: Any) -> dict[str, str]:
        return dict.fromkeys(ids, "db_kind")

    def get_severity_axes(self, ids: Any) -> dict[str, dict[str, str]]:
        return {i: {"scope": "db", "model": "old"} for i in ids}

    def other(self) -> str:
        return "delegated"


AXES = {"scope": "single_org", "impact": "none", "magnitude": "unknown", "recent_action": True}


def _write(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def test_no_args_returns_repo_unchanged() -> None:
    repo = _FakeRepo()
    got, kinds = overlay_if_given(repo, kinds_path=None, axes_path=None)
    assert got is repo
    assert kinds is None


def test_kinds_loaded_and_last_row_wins(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "k.jsonl",
        [{"article_id": "a", "kind": "breach"}, {"article_id": "a", "kind": "advisory"}],
    )
    assert load_kinds_jsonl(p) == {"a": "advisory"}


def test_axes_shape_matches_db_reader() -> None:
    shaped = axes_to_db_shape(AXES, "m")
    assert shaped["scope"] == "single_org"
    assert shaped["recent_action"] == "true"
    assert shaped["model"] == "m"
    assert "victim_size" not in shaped  # 欠測は辞書に含めない


def test_axes_model_filter(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "a.jsonl",
        [
            {"article_id": "a", "model": "new", "axes": AXES},
            {"article_id": "b", "model": "old", "axes": AXES},
        ],
    )
    assert set(load_axes_jsonl(p, "new")) == {"a"}
    assert set(load_axes_jsonl(p)) == {"a", "b"}


def test_overlay_is_strict_and_counts_misses() -> None:
    repo = StagingOverlayRepo(_FakeRepo(), kinds={"a": "breach"}, axes={"a": {"scope": "x"}})
    assert repo.get_article_kinds(["a", "b"]) == {"a": "breach"}
    assert repo.get_severity_axes(["a", "b"]) == {"a": {"scope": "x"}}
    assert repo.misses == {"kinds": 1, "axes": 1}
    assert repo.other() == "delegated"


def test_overlay_without_one_source_falls_through_to_db() -> None:
    repo = StagingOverlayRepo(_FakeRepo(), kinds={"a": "breach"})
    assert repo.get_severity_axes(["z"]) == {"z": {"scope": "db", "model": "old"}}
