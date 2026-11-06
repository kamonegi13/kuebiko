"""学習スクリプトの staging 入力の引数。**省いたとき従来と同一**であることを固定する。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("sklearn")

import importlib  # noqa: E402

# scripts を静的 import すると mypy が scripts 側の型を辿る — 動的に読む
dd: Any = importlib.import_module("scripts.train_deep_dive_ml")
sev: Any = importlib.import_module("scripts.train_severity_s3_model")


def test_load_probes_default_path_and_no_filter(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(sev, "SEVDIR", tmp_path)
    rows = [
        {"article_id": "a", "p_yes": {"cyber": 0.9}, "missing": 5},
        {"article_id": "b", "p_yes": {"cyber": 0.1}},
    ]
    (tmp_path / "probe_m_x.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows), encoding="utf-8"
    )
    assert set(sev.load_probes("m:x")) == {"a", "b"}  # 従来: 欠けの上限なし


def test_load_probes_explicit_path_and_max_missing(tmp_path: Path) -> None:
    p = tmp_path / "p.jsonl"
    rows = [
        {"article_id": "a", "p_yes": {}, "missing": 3},
        {"article_id": "b", "p_yes": {}, "missing": 2},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    assert set(sev.load_probes("ignored", p, max_missing=2)) == {"b"}


def test_deep_dive_build_matrix_default_passes_empty_kinds(monkeypatch: Any) -> None:
    seen: list[dict[str, str]] = []

    def fake(repo: Any, ids: Any, kinds: dict[str, str]) -> dict[str, Any]:
        seen.append(kinds)
        return {}

    monkeypatch.setattr(dd, "build_detect_articles", fake)
    monkeypatch.setattr(dd, "_fetch_meta", lambda repo, ids: {})
    dd.build_matrix([{"article_id": "a", "composite": 1.0}], object())
    dd.build_matrix([{"article_id": "a", "composite": 1.0}], object(), {"a": "breach"})
    assert seen == [{}, {"a": "breach"}]


@pytest.mark.parametrize(
    ("module", "flags"),
    [
        ("scripts.train_detect_ml", ["--kinds", "--axes", "--axes-model", "--out"]),
        ("scripts.train_relation_model", ["--kinds", "--out"]),
        (
            "scripts.train_severity_s3_model",
            ["--items", "--probe-file", "--max-missing", "--kinds"],
        ),
        ("scripts.train_deep_dive_ml", ["--kinds", "--out"]),
    ],
)
def test_staging_flags_are_declared(
    module: str, flags: list[str], monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    import importlib
    import sys

    mod = importlib.import_module(module)
    monkeypatch.setattr(sys, "argv", ["x", "--model", "m", "--help"])
    with pytest.raises(SystemExit):
        mod.main()
    out = capsys.readouterr().out
    assert all(f in out for f in flags)
