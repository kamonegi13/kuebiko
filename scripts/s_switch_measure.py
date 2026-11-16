"""S 切替の関門の指標を staging のモデルと既存の評価データから計算する (S-SWITCH-RETRAIN)。

``s_switch_retrain.py measure`` から呼ばれる I/O 層。純粋な計算は ``src/tools/s_switch_measure.py``。
**GPU・Ollama・DB への書き込みは無い** (detect・relation は DB を読み取りで使う)。sklearn が要る
ので ``uv run --with scikit-learn`` で流す。本番の config / 表には書かない。
"""

# ruff: noqa: E501

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from src.tools.s_switch_measure import (  # noqa: E402
    SEVC_FIXED_THRESHOLD,
    auc_score,
    compare_fold_aucs,
    precision_recall_at,
    same_rate_shifts,
)
from src.tools.staging_inputs import StagingOverlayRepo, load_kinds_jsonl  # noqa: E402

DETECT_REPEATS = 5
DETECT_FOLDS = 5
#: 5 分割の n_test / n_train (補正 t の係数。model_zoo と同じ近似)
DETECT_TEST_TRAIN_RATIO = 1.0 / 4.0
RELATION_MIN_PRECISION = 0.8


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def measure_severity(
    *,
    items_path: Path,
    probe_file: Path,
    probe_model: str,
    kinds_path: Path | None,
    max_missing: int,
    current_probe_file: Path | None = None,
    current_probe_model: str | None = None,
) -> dict[str, Any]:
    """分割外の確率 (trainer と同じ分割・反復) から AUC と、固定の閾値 0.50 での適合・回収。

    ``current_probe_file`` を渡すと、現行の入力 (現行の S の確率 + items の種別) を**同じ手順**で
    測った値を ``current`` に入れる。関門はこれを基準にする (CLAUDE.md §7 の 0.916 / 0.69 / 0.83 は
    丸めた値で、同じ手順の現行は 0.9158 / 0.686 / 0.831 = 適合で現行そのものが定数を下回る。11-10 実測)。
    ``kinds_missing`` = 付け直しの種別に無い学習記事の数 (種別が空になり分布が変わる。0 であること)。
    """
    sev = importlib.import_module("scripts.train_severity_s3_model")
    items = _jsonl(items_path)
    result: dict[str, Any] = {}
    if current_probe_file is not None and current_probe_model:
        result["current"] = _severity_metrics(
            sev,
            items,
            sev.load_probes(current_probe_model, current_probe_file, max_missing=max_missing),
        )
    if kinds_path is not None:
        staged = load_kinds_jsonl(kinds_path)
        result["kinds_missing"] = float(sum(r["article_id"] not in staged for r in items))
        items = [{**r, "event_kind": staged.get(r["article_id"], "")} for r in items]
    probes = sev.load_probes(probe_model, probe_file, max_missing=max_missing)
    return {**_severity_metrics(sev, items, probes), **result}


def _severity_metrics(sev: Any, items: list[dict[str, Any]], probes: Any) -> dict[str, float]:
    _, _, x, y = sev.build_dataset(items, probes)
    p = sev.oof_probabilities(x, y)
    precision, recall = precision_recall_at(y, p, SEVC_FIXED_THRESHOLD)
    return {
        "auc": auc_score(y, p),
        "precision": precision,
        "recall": recall,
        "threshold": SEVC_FIXED_THRESHOLD,
        "n": float(len(y)),
    }


def measure_relation(*, gold: Path, kinds_path: Path | None) -> dict[str, float]:
    """新 (staging の種別) の適合・回収と、現行の入力 (DB の種別) の同じ手順での回収。

    現行の本番モデルは全件で学習済みで同じ fold に当てると漏れるので、**現行と同じ入力で同じ
    fold・同じ閾値の決め方 (適合 >= 0.80 で回収最大) を回した回収**を ``current_recall`` とする。
    """
    rel = importlib.import_module("scripts.train_relation_model")
    from src.storage.run_history import RunHistoryRepository

    plain = RunHistoryRepository()
    staged = (
        StagingOverlayRepo(plain, kinds=load_kinds_jsonl(kinds_path))
        if kinds_path is not None
        else plain
    )
    x_new, y_new = rel.build_xy(staged, gold)
    x_cur, y_cur = rel.build_xy(plain, gold)
    if len(y_new) == 0 or len(y_cur) == 0:
        raise SystemExit("組 0 件 — DATABASE_URL 無し (空 SQLite) を疑う")
    _, prec, rec = rel.choose_threshold(
        rel.cv_probabilities(x_new, y_new), y_new, min_precision=RELATION_MIN_PRECISION
    )
    _, _, cur_rec = rel.choose_threshold(
        rel.cv_probabilities(x_cur, y_cur), y_cur, min_precision=RELATION_MIN_PRECISION
    )
    return {"precision": prec, "recall": rec, "current_recall": cur_rec}


def _pair_probs(
    model_path: Path,
    pairs: list[dict[str, Any]],
    feats: dict[str, Any],
    kinds: dict[str, str],
    overrides: dict[tuple[str, str], bool | None] | None,
) -> tuple[np.ndarray, float]:
    tp = importlib.import_module("scripts.train_pair_model")
    from src.eventnews.pair_model import load_model

    model = load_model(str(model_path))
    if model is None:
        raise SystemExit(f"pair モデルを読めない: {model_path}")
    scores, known = tp.llm_columns(pairs, feats["llm_26b"], overrides)
    x = np.hstack(
        [
            np.array(feats["X"]),
            tp.kind_matrix(pairs, kinds),
            scores.reshape(-1, 1),
            known.reshape(-1, 1),
        ]
    )
    probs = np.array([model.probability([float(v) for v in row]) for row in x])
    return probs, model.threshold_llm_on


def measure_pair(
    *,
    current_model: Path,
    staging_model: Path,
    labels: Path,
    samples: list[Path],
    features: Path,
    current_kinds: Path,
    new_kinds: Path,
    new_llm_scores: Path | None,
) -> dict[str, Any]:
    """365 対の cos の帯ごとの same 率のずれと、正解 (labels の same 添字) に対する適合率。

    ⚠ 現行も新も同じ 365 対で学習した木なので適合率は学習標本の上の値 (両者を同じ条件で比べる
    ためのもの。汎化の指標ではない)。入力は train_pair_model.py の既定と同じ (LLM は伏せない)。
    """
    tp = importlib.import_module("scripts.train_pair_model")
    pairs = [p for f in samples for p in json.loads(f.read_text(encoding="utf-8"))]
    feats = json.loads(features.read_text(encoding="utf-8"))
    lab = json.loads(labels.read_text(encoding="utf-8"))
    y = np.zeros(len(pairs), dtype=int)
    y[lab["same"]] = 1
    overrides = tp.load_llm_scores(new_llm_scores) if new_llm_scores else None
    cur_p, cur_t = _pair_probs(current_model, pairs, feats, tp.load_kinds(current_kinds), None)
    new_p, new_t = _pair_probs(staging_model, pairs, feats, tp.load_kinds(new_kinds), overrides)
    cur_same, new_same = cur_p >= cur_t, new_p >= new_t
    cos = np.array(feats["X"], dtype=float)[:, 0]  # 特徴の先頭は cos (pair_features.FEATURE_NAMES)
    shifts = same_rate_shifts(cos, new_same, cur_same)
    return {
        "same_rate_shift_by_cos_band": shifts,
        "precision": precision_recall_at(y, new_same.astype(float), 0.5)[0],
        "current_precision": precision_recall_at(y, cur_same.astype(float), 0.5)[0],
    }


def _fold_aucs(x: np.ndarray, y: np.ndarray) -> list[float]:
    """detect の本番と同じ学習器 (標準化 + LR C=0.3 balanced) の fold ごとの AUC (全腕で同じ分割)。"""
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import StratifiedKFold
    from sklearn.preprocessing import StandardScaler

    det = importlib.import_module("scripts.train_detect_ml")
    out: list[float] = []
    for rep in range(DETECT_REPEATS):
        for tr, te in StratifiedKFold(DETECT_FOLDS, shuffle=True, random_state=rep).split(x, y):
            sc = StandardScaler().fit(x[tr])
            clf = LogisticRegression(C=det._C, max_iter=3000, class_weight="balanced")  # noqa: SLF001
            clf.fit(sc.transform(x[tr]), y[tr])
            out.append(auc_score(y[te], clf.predict_proba(sc.transform(x[te]))[:, 1]))
    return out


def _arm_matrix(repo: Any, ids: list[str], kinds: dict[str, str]) -> np.ndarray:
    from src.synthesis.grounded.detect_features import feature_vector
    from src.synthesis.grounded.detect_ml import build_detect_articles

    arts = build_detect_articles(repo, ids, kinds)
    return np.array([feature_vector(arts[a]) for a in ids], dtype=float)


def _diff_summary(a: list[float], b: list[float]) -> dict[str, float]:
    r = compare_fold_aucs(a, b, test_train_ratio=DETECT_TEST_TRAIN_RATIO)
    return {
        "mean_diff": r["mean_diff"],
        "auc_diff_ci_lower": r["ci_lower"],
        "auc_diff_ci_upper": r["ci_upper"],
        "p_worse": r["p_worse"],
    }


def measure_detect(
    *,
    gold_path: Path,
    current_kind_files: tuple[Path, ...],
    new_kinds: Path,
    new_axes: Path,
    axes_model: str,
    control_kinds: Path | None,
    control_axes: Path | None = None,
    control_axes_model: str | None = None,
) -> dict[str, Any]:
    """goldset の同じ記事・同じ y・同じ fold で 現行 / 対照 / 新 の AUC を比べる。

    腕: 現行 = 現行の種別ファイル (欠けは other) + DB の軸 / 対照 = 現行の S で付け直した種別 (欠け
    解消) + DB の軸 / 新 = 新 S の種別 + 新 S の軸。新 vs 対照が S の効果、対照 vs 現行が欠けを埋めた
    効果 (参考)。対照が無ければ新 vs 現行だけ (交絡あり、ゲートが警告する)。
    開設の件数は審判が要るので None (手で足す)。
    ``control_axes`` (現行の S で付け直した軸) を渡すと対照の軸も付け直しの値にする。新の軸は
    付け直し (本文入力・今の prompt) なので、対照が DB の軸のままだと「S の効果」に付け直しの
    揺れ (同じ S でも 7 欄中 1 欄が変わる例あり、11-10 試走) が混ざる。
    """
    from src.storage.run_history import RunHistoryRepository
    from src.tools.staging_inputs import load_axes_jsonl

    gold = {str(g["article_id"]): g for g in _jsonl(gold_path)}
    plain = RunHistoryRepository()
    cur_kinds = {
        str(r["article_id"]): str(r["kind"]) for f in current_kind_files for r in _jsonl(f)
    }
    new_k = load_kinds_jsonl(new_kinds)
    ctrl_k = load_kinds_jsonl(control_kinds) if control_kinds else None
    ctrl_axes = load_axes_jsonl(control_axes, control_axes_model) if control_axes else None
    axes = load_axes_jsonl(new_axes, axes_model)
    have_db = plain.get_severity_axes(sorted(gold))
    ids = sorted(a for a in gold if a in axes and a in have_db and a in new_k)
    if ctrl_k is not None:
        ids = [a for a in ids if a in ctrl_k]
    if ctrl_axes is not None:
        ids = [a for a in ids if a in ctrl_axes]
    if not ids:
        raise SystemExit("記事 0 件 — DATABASE_URL か staging の軸・種別を疑う")
    y = np.array([int(bool(gold[a]["gold_open"])) for a in ids])
    x_cur = _arm_matrix(plain, ids, cur_kinds)
    x_new = _arm_matrix(StagingOverlayRepo(plain, kinds=new_k, axes=axes), ids, new_k)
    auc_cur, auc_new = _fold_aucs(x_cur, y), _fold_aucs(x_new, y)
    result: dict[str, Any] = {
        "n_articles": len(ids),
        "mean_auc_current": float(np.mean(auc_cur)),
        "mean_auc_new": float(np.mean(auc_new)),
    }
    if ctrl_k is None:
        result.update(_diff_summary(auc_cur, auc_new))
        result.update({"new_only_opens": None, "current_only_opens": None})
        return result
    x_ctrl = _arm_matrix(StagingOverlayRepo(plain, kinds=ctrl_k, axes=ctrl_axes), ids, ctrl_k)
    result["control_axes"] = "relabeled" if ctrl_axes is not None else "db"
    auc_ctrl = _fold_aucs(x_ctrl, y)
    result["mean_auc_control"] = float(np.mean(auc_ctrl))
    result["new_vs_control"] = {
        **_diff_summary(auc_ctrl, auc_new),
        "new_only_opens": None,
        "control_only_opens": None,
    }
    result["control_vs_current"] = _diff_summary(auc_cur, auc_ctrl)
    return result
