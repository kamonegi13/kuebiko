#!/usr/bin/env python3
# ruff: noqa: E501  (日本語の文は幅 2 で数えられ長く見える)
"""記事ペア判定モデル (config/models/pair_model.json) を学習して書き出す (CPU のみ、LLM は呼ばない)。

旧 ``data/eval/train_v5.py`` (v5 = 決定論 19 + 種別対 4 + LLM 2) を ``scripts/`` に正式に置いたもの。
書式・乱数・閾値は v5 と同一 — 既定の入力で学ぶと本番の ``pair_model.json`` の木と一致する。

* 特徴の並びは **src の FEATURE_NAMES と kind_pair_features を直接使う** (SSoT を守る)。
* LLM の判定 (``llm_same`` / ``llm_known``) は学習時に 30% を「不明」(0, 0) に伏せる。
  伏せずに学ぶと LLM が落ちたとき沈む (src/eventnews/pair_model.py の実測)。
* S の切替では ``--kinds`` (新しい S で付け直した種別) と ``--llm-scores`` (新しい判定器で当て直した
  対の判定) を渡し、``--llm-model`` を JSON の ``trained_llm_models`` に残す。
  決定論の 19 列は S に依存しない (``--features`` の既存の値を使う)。

    uv run --with scikit-learn python scripts/train_pair_model.py --out config/models/pair_model.json.staging

入力 (既定は v5 の学習データ): data/eval/labels_verified.json (same/diff の添字)・
eval_sample{,2,3}.json (対)・event_kind.json (記事 id → 種別)・features_v3.json (決定論 19 列 +
元の判定 llm_26b)。出力は本番形式 (sklearn 非依存の木 JSON)。**既定の出力先は本番の config**
なので、S 切替では必ず ``--out`` に staging を渡す。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.eventnews.event_kind import kind_pair_features  # noqa: E402
from src.eventnews.pair_features import FEATURE_NAMES  # noqa: E402

D = Path("data/eval")
DEFAULT_OUT = Path("config/models/pair_model.json")
LLM_COLUMNS = ("llm_same", "llm_known")
MASK_RATE = 0.30
MASK_SEED = 20260901
N_TREES = 400
MIN_LEAF = 2
THRESHOLD_LLM_ON = 0.65
THRESHOLD_LLM_OFF = 0.62
N_REFERENCE_POINTS = 5
MODEL_VERSION = 2
#: v5 の既定の標本 (連結順 = labels_verified.json の添字)
DEFAULT_SAMPLES = ("eval_sample.json", "eval_sample2.json", "eval_sample3.json")


def mask_llm(
    scores: np.ndarray, known: np.ndarray, *, rate: float = MASK_RATE, seed: int = MASK_SEED
) -> tuple[np.ndarray, np.ndarray]:
    """判定の一部を「不明」(score 0・known 0) に伏せる。元の配列は書き換えない。"""
    rng = np.random.default_rng(seed)
    hide = rng.random(len(scores)) < rate
    return np.where(hide, 0.0, scores), np.where(hide, 0.0, known)


def llm_columns(
    pairs: Sequence[dict[str, Any]],
    base_scores: Sequence[float],
    overrides: dict[tuple[str, str], bool | None] | None,
) -> tuple[np.ndarray, np.ndarray]:
    """対ごとの (判定, 既知)。``overrides`` があれば新しい判定器の値で置き換える (None = 判定不能)。"""
    scores = np.array(base_scores, dtype=float)
    known = np.ones(len(pairs))
    if overrides is None:
        return scores, known
    for i, p in enumerate(pairs):
        verdict = overrides.get((str(p["a"]), str(p["b"])))
        scores[i] = float(bool(verdict)) if verdict is not None else 0.0
        known[i] = 0.0 if verdict is None else 1.0
    return scores, known


def kind_matrix(pairs: Sequence[dict[str, Any]], kinds: dict[str, str]) -> np.ndarray:
    return np.array(
        [
            kind_pair_features(kinds.get(str(p["a"]), "other"), kinds.get(str(p["b"]), "other"))
            for p in pairs
        ],
        dtype=float,
    )


def export_trees(estimators: Sequence[Any]) -> list[dict[str, list[float]]]:
    """sklearn の木 → 本番の JSON。⚠ 閾値は丸めない (境界で分岐が反転する)。"""
    trees = []
    for est in estimators:
        t = est.tree_
        val = t.value.reshape(t.value.shape[0], -1)
        tot = val.sum(axis=1)
        p1 = np.where(tot > 0, val[:, 1] / np.maximum(tot, 1e-9), 0.0)
        trees.append(
            {
                "left": t.children_left.tolist(),
                "right": t.children_right.tolist(),
                "feature": t.feature.tolist(),
                "threshold": [float(v) for v in t.threshold],
                "proba": [float(v) for v in p1],
            }
        )
    return trees


def assemble_model(
    *,
    names: list[str],
    n_samples: int,
    trees: list[dict[str, list[float]]],
    xf: np.ndarray,
    proba: np.ndarray,
    trained_at: str,
    trained_llm_models: list[str],
) -> dict[str, Any]:
    """本番の ``pair_model.json`` と同じ欄・同じ並びで組む (欄の SSoT は pair_model.py の読み側)。"""
    ref = range(min(N_REFERENCE_POINTS, len(xf)))
    return {
        "version": MODEL_VERSION,
        "trained_llm_models": trained_llm_models,
        "trained_at": trained_at,
        "n_samples": n_samples,
        "feature_names": names,
        "mask_rate": MASK_RATE,
        "threshold_llm_on": THRESHOLD_LLM_ON,
        "threshold_llm_off": THRESHOLD_LLM_OFF,
        "reference_points": {
            "X": [xf[i].tolist() for i in ref],
            "proba": [float(proba[i]) for i in ref],
        },
        "trees": trees,
    }


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_kinds(path: Path) -> dict[str, str]:
    """``.jsonl`` = ``{"article_id","kind"}`` の行、それ以外 = ``{記事 id: 種別}`` の JSON。"""
    if path.suffix == ".jsonl":
        rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        return {str(r["article_id"]): str(r["kind"]) for r in rows}
    return {str(k): str(v) for k, v in _load_json(path).items()}


def load_llm_scores(path: Path) -> dict[tuple[str, str], bool | None]:
    """``{"a","b","same": true|false|null}`` の jsonl → 対 → 判定 (null = 判定不能)。"""
    out: dict[tuple[str, str], bool | None] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            out[(str(r["a"]), str(r["b"]))] = None if r["same"] is None else bool(r["same"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--labels", type=Path, default=D / "labels_verified.json")
    ap.add_argument("--samples", type=Path, nargs="+", default=[D / n for n in DEFAULT_SAMPLES])
    ap.add_argument("--kinds", type=Path, default=D / "event_kind.json")
    ap.add_argument("--features", type=Path, default=D / "features_v3.json")
    ap.add_argument("--llm-scores", type=Path, default=None, help="新しい判定器の対の判定 (jsonl)")
    ap.add_argument("--llm-model", default="", help="対の判定に使ったモデル (trained_llm_models)")
    ap.add_argument("--trained-at", default="2026-09-03")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from sklearn.ensemble import RandomForestClassifier

    lab = _load_json(args.labels)
    pairs = [p for f in args.samples for p in _load_json(f)]
    feats = _load_json(args.features)
    y = np.zeros(len(pairs))
    y[lab["same"]] = 1.0
    x = np.hstack([np.array(feats["X"]), kind_matrix(pairs, load_kinds(args.kinds))])
    if x.shape[1] != len(FEATURE_NAMES) or len(feats["X"]) != len(pairs):
        print(
            f"⚠ 形が合わない: X {x.shape}, 特徴名 {len(FEATURE_NAMES)}, 対 {len(pairs)}",
            file=sys.stderr,
        )
        return 1
    overrides = load_llm_scores(args.llm_scores) if args.llm_scores else None
    scores, known = llm_columns(pairs, feats["llm_26b"], overrides)
    s, k = mask_llm(scores, known)
    xf = np.hstack([x, s.reshape(-1, 1), k.reshape(-1, 1)])

    model = RandomForestClassifier(N_TREES, min_samples_leaf=MIN_LEAF, random_state=0).fit(xf, y)
    out = assemble_model(
        names=[*FEATURE_NAMES, *LLM_COLUMNS],
        n_samples=len(y),
        trees=export_trees(model.estimators_),
        xf=xf,
        proba=model.predict_proba(xf)[:, 1],
        trained_at=args.trained_at,
        trained_llm_models=[args.llm_model] if args.llm_model else [],
    )
    print(
        f"特徴 {len(out['feature_names'])} / 木 {len(out['trees'])} / "
        f"ラベル 同一 {int(y.sum())} 別 {int((1 - y).sum())}"
    )
    if args.dry_run:
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    print(f"書込: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
