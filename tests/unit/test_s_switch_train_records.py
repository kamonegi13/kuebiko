"""各 trainer が kinds_model を書き出す (sklearn 要)。既定の入力では null / 欄なしで従来と同じ。"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import numpy as np
import pytest

pytest.importorskip("sklearn")

det: Any = importlib.import_module("scripts.train_detect_ml")
rel: Any = importlib.import_module("scripts.train_relation_model")
pair: Any = importlib.import_module("scripts.train_pair_model")
retrain: Any = importlib.import_module("scripts.s_switch_retrain")


def _fit_lr() -> tuple[Any, Any]:
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    x = np.array([[0.0, 1.0], [1.0, 0.0], [0.2, 0.9], [0.9, 0.1]])
    y = np.array([0, 1, 0, 1])
    sc = StandardScaler().fit(x)
    return sc, LogisticRegression().fit(sc.transform(x), y)


def test_detect_export_records_kinds_model_or_null() -> None:
    sc, clf = _fit_lr()
    assert det.export(sc, clf, 0.5, "s:a", "s:k")["kinds_model"] == "s:k"
    assert det.export(sc, clf, 0.5, "s:a")["kinds_model"] is None  # 既定 = 記録なし


def test_relation_export_records_kinds_model_or_null() -> None:
    from sklearn.ensemble import RandomForestClassifier

    x = np.array([[0.0, 1.0], [1.0, 0.0], [0.2, 0.9], [0.9, 0.1]])
    model = RandomForestClassifier(3, random_state=0).fit(x, [0, 1, 0, 1])
    assert rel.export(model, 0.5, 4, "s:k")["kinds_model"] == "s:k"
    assert rel.export(model, 0.5, 4)["kinds_model"] is None


def test_pair_assemble_adds_the_field_only_when_given() -> None:
    kw: dict[str, Any] = {
        "names": ["a"],
        "n_samples": 1,
        "trees": [],
        "xf": np.zeros((1, 1)),
        "proba": np.zeros(1),
        "trained_at": "2026-01-01",
        "trained_llm_models": [],
    }
    assert "kinds_model" not in pair.assemble_model(**kw)  # 既定の入力は従来と同一
    assert pair.assemble_model(**kw, kinds_model="s:k")["kinds_model"] == "s:k"


@pytest.mark.parametrize(
    "module",
    [
        "scripts.train_detect_ml",
        "scripts.train_relation_model",
        "scripts.train_severity_s3_model",
        "scripts.train_deep_dive_ml",
        "scripts.train_pair_model",
    ],
)
def test_kinds_model_flag_is_declared(
    module: str, monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    mod = importlib.import_module(module)
    monkeypatch.setattr(sys, "argv", ["x", "--model", "m", "--help"])
    with pytest.raises(SystemExit):
        mod.main()
    assert "--kinds-model" in capsys.readouterr().out


def test_control_job_trains_detect_from_control_kinds_into_staging_control() -> None:
    name, argv, out, _ = retrain.build_control_job("s:cur", Path("/x/kinds_s_cur.jsonl"))
    assert name == "detect_control"
    assert argv[argv.index("--kinds") + 1] == "/x/kinds_s_cur.jsonl"
    assert argv[argv.index("--kinds-model") + 1] == "s:cur"
    assert "--axes" not in argv  # 軸は DB (現行の S) のまま = 欠けの効果だけを分ける
    assert out.name == "detect_model.json.staging_control"


def test_write_metrics_keeps_hand_entered_values(tmp_path: Path, monkeypatch: Any) -> None:
    import json

    monkeypatch.setattr(retrain, "STAGING_DIR", tmp_path)
    (tmp_path / "train_metrics.json").write_text(
        json.dumps({"detect": {"new_vs_control": {"new_only_opens": 13}}}), encoding="utf-8"
    )
    retrain._write_metrics({"detect": {"new_vs_control": {"new_only_opens": None, "p_worse": 0.1}}})
    got = json.loads((tmp_path / "train_metrics.json").read_text(encoding="utf-8"))
    assert got["detect"]["new_vs_control"] == {"new_only_opens": 13, "p_worse": 0.1}


def test_measure_severity_smoke_on_synthetic_items(tmp_path: Path) -> None:
    """合成データで、AUC・固定閾値の適合/回収が trainer と同じ経路で出る (本文なし・架空の id)。"""
    import json

    from src.cti import severity_c as sc

    measure: Any = importlib.import_module("scripts.s_switch_measure")
    rng = np.random.default_rng(0)
    items, probes = [], []
    for i in range(60):
        s3 = i % 3 == 0
        base = {
            "article_id": f"a{i}",
            "category": "malware",
            "article_type": "incident",
            "event_kind": "breach",
            "on_kev": False,
            "max_cvss": 0.0,
            "loss_usd": 0.0,
            "n_cves": 0,
            "gold_severity": "S3" if s3 else "S1",
        }
        items.append(base)
        p = {
            q: float(np.clip(rng.normal(0.8 if s3 else 0.2, 0.1), 0.01, 0.99))
            for q in sc.QUESTION_IDS
        }
        probes.append({"article_id": f"a{i}", "p_yes": p, "missing": 0})
    items_path, probe_path = tmp_path / "items.jsonl", tmp_path / "probe.jsonl"
    items_path.write_text("\n".join(json.dumps(r) for r in items), encoding="utf-8")
    probe_path.write_text("\n".join(json.dumps(r) for r in probes), encoding="utf-8")
    got = measure.measure_severity(
        items_path=items_path,
        probe_file=probe_path,
        probe_model="m:x",
        kinds_path=None,
        max_missing=2,
    )
    assert got["n"] == 60.0 and got["threshold"] == 0.5
    assert got["auc"] > 0.95 and got["precision"] > 0.9 and got["recall"] > 0.9
