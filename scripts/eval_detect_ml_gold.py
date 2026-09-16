#!/usr/bin/env python3
"""detect の ML 化を **審判ラベル** (build_detect_goldset.py) で交差検証する (2026-09-17)。

09-10 (SYNTHESIS §36) の「detect ML 棄却」は、正例が「開設→成長」という現行由来の
結果ラベルだったための誤結論。盲検の審判ラベル (importance / trackable / open / watch) を
目標にすると、同じ特徴量でも現行 detect を上回る。事象種別 (event_kind) が追跡価値の
主要な担い手。

入力 (すべて data/mlx/): detect_goldset.jsonl (審判) / detect_features.json (メタ) /
detect_llm_feats.jsonl (LLM 採点) / detect_gold_kinds.jsonl (event_kind) / detect_embeds.npz。
出力: 標準出力に 特徴量セット × モデル の AUC / AP / 同量運転の precision・recall。

⚠ n≈190・正 30 の小標本。層化 5-fold × 3 seed の平均で、時系列分割ではない。
使い方: uv run python scripts/eval_detect_ml_gold.py
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.ensemble import RandomForestClassifier  # type: ignore[import-untyped]
from sklearn.linear_model import LogisticRegression  # type: ignore[import-untyped]
from sklearn.metrics import average_precision_score, roc_auc_score  # type: ignore[import-untyped]
from sklearn.model_selection import (  # type: ignore[import-untyped]
    StratifiedKFold,
    cross_val_predict,
)
from sklearn.pipeline import make_pipeline  # type: ignore[import-untyped]
from sklearn.preprocessing import StandardScaler  # type: ignore[import-untyped]

D = Path("data/mlx")
KINDS = ("advisory", "exploitation", "breach", "stats", "roundup", "other")
NOVELTY = ("new", "followup", "rereport")
META_NUM = ("importance", "age_days", "n_actor", "n_cve", "n_malware", "n_victim", "n_entities")
SEEDS = 3
FOLDS = 5


def _jsonl(path: Path) -> dict[str, dict[str, Any]]:
    return {
        str(json.loads(line)["article_id"]): json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def build_feature_sets(
    meta: dict[str, dict[str, Any]],
    llm: dict[str, dict[str, Any]],
    kind: dict[str, str],
    emb: dict[str, np.ndarray],
    ids: list[str],
) -> dict[str, Callable[[str], list[float]]]:
    """特徴量セット名 → (article_id → ベクトル)。one-hot の語彙は ids から固定する。"""
    tiers = sorted({str(meta[a]["f"]["tier"]) for a in ids})
    cats = sorted({str(meta[a]["f"]["category"]) for a in ids})

    def f_meta(a: str) -> list[float]:
        f = meta[a]["f"]
        return (
            [float(f[k]) for k in META_NUM]
            + [float(f["tier"] == t) for t in tiers]
            + [float(f["category"] == c) for c in cats]
        )

    def f_llm(a: str) -> list[float]:
        r = llm[a]
        return [float(r[k]) for k in ("open_score", "mission_fit", "severity")] + [
            float(r["novelty"] == n) for n in NOVELTY
        ]

    def f_kind(a: str) -> list[float]:
        return [float(kind[a] == k) for k in KINDS]

    return {
        "meta+llm (09-10 相当)": lambda a: f_meta(a) + f_llm(a),
        "kind のみ": f_kind,
        "kind+meta": lambda a: f_kind(a) + f_meta(a),
        "kind+meta+llm": lambda a: f_kind(a) + f_meta(a) + f_llm(a),
        "kind+meta+llm+emb": lambda a: (
            f_kind(a) + f_meta(a) + f_llm(a) + [float(x) for x in emb[a]]
        ),
    }


def _models(seed: int) -> list[tuple[str, Any]]:
    return [
        (
            "logreg",
            make_pipeline(
                StandardScaler(),
                LogisticRegression(C=0.3, max_iter=3000, class_weight="balanced"),
            ),
        ),
        (
            "rf",
            RandomForestClassifier(
                200, min_samples_leaf=3, class_weight="balanced", random_state=seed, n_jobs=4
            ),
        ),
    ]


def evaluate(x: np.ndarray, y: np.ndarray, *, same_volume: int, skip_rf: bool) -> list[str]:
    lines: list[str] = []
    for name in ("logreg", "rf"):
        if skip_rf and name == "rf":
            continue
        auc: list[float] = []
        ap: list[float] = []
        prec: list[float] = []
        rec: list[float] = []
        for seed in range(SEEDS):
            model = dict(_models(seed))[name]
            cv = StratifiedKFold(FOLDS, shuffle=True, random_state=seed)
            p = cross_val_predict(model, x, y, cv=cv, method="predict_proba")[:, 1]
            auc.append(float(roc_auc_score(y, p)))
            ap.append(float(average_precision_score(y, p)))
            top = np.argsort(-p)[:same_volume]
            prec.append(float(y[top].mean()))
            rec.append(float(y[top].sum() / y.sum()))
        lines.append(
            f"    {name:6s} AUC {np.mean(auc):.2f}  AP {np.mean(ap):.2f}  "
            f"同量 {same_volume} 件: precision {np.mean(prec):.2f} recall {np.mean(rec):.2f}"
        )
    return lines


def main() -> int:
    gold = _jsonl(D / "detect_goldset.jsonl")
    meta = {str(r["article_id"]): r for r in json.loads((D / "detect_features.json").read_text())}
    llm = _jsonl(D / "detect_llm_feats.jsonl")
    kind = {a: str(r["kind"]) for a, r in _jsonl(D / "detect_gold_kinds.jsonl").items()}
    z = np.load(D / "detect_embeds.npz")
    emb = {str(a): z["X"][i] for i, a in enumerate(z["article_ids"])}
    ids = [a for a in gold if a in meta and a in llm and a in kind and a in emb]
    opened = [a for a in ids if meta[a]["decision"] == "opened"]
    print(f"n {len(ids)} / 現行 opened {len(opened)}")
    sets = build_feature_sets(meta, llm, kind, emb, ids)
    targets = {
        "gold_open": np.array([int(bool(gold[a]["gold_open"])) for a in ids]),
        "trackable>=2": np.array([int(int(gold[a]["trackable"]) >= 2) for a in ids]),
    }
    for tname, y in targets.items():
        hit = sum(int(y[ids.index(a)]) for a in opened)
        print(f"\n== {tname} (正 {int(y.sum())}) — 現行 detect: {hit}/{len(opened)} ==")
        for sname, fn in sets.items():
            x = np.array([fn(a) for a in ids], dtype=float)
            print(f"  {sname}")
            for line in evaluate(x, y, same_volume=len(opened), skip_rf="emb" in sname):
                print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
