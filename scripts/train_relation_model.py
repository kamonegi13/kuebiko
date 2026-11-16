#!/usr/bin/env python3
"""事象どうしの関係の分類器 (同じ出来事の系統か) を学習し、木を JSON に書き出す (2026-09-27)。

学習データ = Opus 盲検の事象の組 (定義 v3、data/mlx/event_relations_goldset_v3.jsonl)。
陽性 = 続報・側面・包含。**特徴量は本番の関数** (`relation_pair_features.relation_feature_vector`)
で作る — 学習と本番の計算を食い違わせない。閾値は交差検証の確率から ``--min-precision`` を
満たす中で回収が最大の点。

sklearn は本番 image に無いので **ホストで DATABASE_URL を本番 PG に向けて** 走らせる:
    DATABASE_URL=postgresql://kuebiko:...@127.0.0.1:5433/kuebiko \\
        uv run python scripts/train_relation_model.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
from sklearn.ensemble import RandomForestClassifier  # type: ignore[import-untyped]  # noqa: E402
from sklearn.model_selection import StratifiedKFold  # type: ignore[import-untyped]  # noqa: E402

from src.eventnews.relation_features import load_event_features  # noqa: E402
from src.eventnews.relation_model import DEFAULT_MODEL_PATH, RelationModel  # noqa: E402
from src.eventnews.relation_pair_features import (  # noqa: E402
    FEATURE_NAMES,
    centroid_cos,
    load_centroids,
    relation_feature_vector,
)
from src.eventnews.relations import _INDEXED, INCIDENT_TYPES, WINDOW_DAYS, _df  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.tools.staging_inputs import overlay_if_given, resolve_kinds_model  # noqa: E402

GOLD = Path("data/mlx/event_relations_goldset_v3.jsonl")
N_TREES = 300
MIN_LEAF = 3


def build_xy(repo: RunHistoryRepository, gold: Path) -> tuple[Any, Any]:
    labels = {}
    for line in gold.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            labels[r["key"]] = r["label"]
    events = load_event_features(repo, days=WINDOW_DAYS)
    by_id = {e.item_id: e for e in events}
    df = {attr: _df(events, attr) for attr in _INDEXED}
    wanted = [by_id[i] for k in labels for i in k.split("|") if i in by_id]
    centroids = load_centroids(repo, list({e.item_id: e for e in wanted}.values()))
    xs, ys = [], []
    for key, label in labels.items():
        a_id, b_id = key.split("|")
        if a_id not in by_id or b_id not in by_id:
            continue
        cos = centroid_cos(centroids, a_id, b_id)
        xs.append(relation_feature_vector(by_id[a_id], by_id[b_id], df, cos))
        ys.append(int(label in INCIDENT_TYPES))
    print(f"ラベル {len(labels)} / 窓内で特徴量を作れた組 {len(ys)}")
    return np.array(xs, dtype=np.float32), np.array(ys)


def choose_threshold(p: Any, y: Any, *, min_precision: float) -> tuple[float, float, float]:
    """交差検証の確率から、精度が下限以上で回収が最大の閾値 (純粋関数)。"""
    best = (1.0, 0.0, 0.0)
    for t in np.arange(0.30, 0.96, 0.01):
        sel = p >= t
        if not sel.any():
            continue
        prec = float((sel & (y == 1)).sum() / sel.sum())
        rec = float((sel & (y == 1)).sum() / max(1, y.sum()))
        if prec >= min_precision and rec > best[2]:
            best = (float(t), prec, rec)
    return best


def make_forest(seed: int = 0) -> Any:
    return RandomForestClassifier(
        N_TREES, min_samples_leaf=MIN_LEAF, class_weight="balanced", random_state=seed
    )


def cv_probabilities(x: Any, y: Any) -> Any:
    """層化 5 分割の分割外の確率 (閾値の決定と、関門の適合・回収・現行の回収が共有する)。"""
    p = np.zeros(len(y))
    for tr, te in StratifiedKFold(5, shuffle=True, random_state=0).split(x, y):
        p[te] = make_forest().fit(x[tr], y[tr]).predict_proba(x[te])[:, 1]
    return p


def export(model: Any, threshold: float, n: int, kinds_model: str | None = None) -> dict[str, Any]:
    trees = []
    for est in model.estimators_:
        t = est.tree_
        val = t.value.reshape(t.value.shape[0], -1)
        tot = val.sum(axis=1)
        p1 = np.where(tot > 0, val[:, 1] / np.maximum(tot, 1e-9), 0.0)
        trees.append(
            {
                "left": t.children_left.tolist(),
                "right": t.children_right.tolist(),
                "feature": t.feature.tolist(),
                "threshold": [float(v) for v in t.threshold],  # ⚠ 丸めない (境界で分岐が反転する)
                "proba": [float(v) for v in p1],
            }
        )
    return {
        "version": 1,
        # 種別 (kinds_*) を付けた S。DB の article_kinds (混在・過去値) なら null
        "kinds_model": kinds_model,
        "n_samples": n,
        "positive": sorted(INCIDENT_TYPES),
        "feature_names": list(FEATURE_NAMES),
        "window_days": WINDOW_DAYS,
        "threshold": threshold,
        "trees": trees,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gold", type=Path, default=GOLD)
    ap.add_argument("--min-precision", type=float, default=0.8)
    ap.add_argument("--out", type=Path, default=DEFAULT_MODEL_PATH)
    ap.add_argument("--dry-run", action="store_true")
    # S 切替の付け直し (staging)。省けば従来どおり (種別 = DB の article_kinds)
    ap.add_argument("--kinds", type=Path, default=None, help="種別の jsonl (article_id, kind)")
    ap.add_argument(
        "--kinds-model",
        default=None,
        help="--kinds の S の名前 (JSON の kinds_model)。省略時は jsonl の model 欄から推定",
    )
    args = ap.parse_args()
    repo, _ = overlay_if_given(RunHistoryRepository(), kinds_path=args.kinds, axes_path=None)
    x, y = build_xy(repo, args.gold)
    if args.kinds is not None:
        print(f"staging の種別 被覆: 欠け {repo.misses['kinds']} 件 (欠けは種別なしとして学ぶ)")
    if len(y) == 0:
        print("⚠ 組 0 件 — DATABASE_URL 無し (空 SQLite) を疑う", file=sys.stderr)
        return 1

    p = cv_probabilities(x, y)
    t, prec, rec = choose_threshold(p, y, min_precision=args.min_precision)
    print(f"組 {len(y)} / 同じ出来事の系統 {int(y.sum())}")
    print(f"閾値 {t:.2f} (交差検証: 精度 {100 * prec:.0f}% 回収 {100 * rec:.0f}%)")
    model = make_forest().fit(x, y)
    out = export(model, t, len(y), resolve_kinds_model(args.kinds_model, args.kinds))
    mine = RelationModel(
        feature_names=tuple(out["feature_names"]), threshold=t, _trees=tuple(out["trees"])
    )
    diff = float(np.max(np.abs(mine.probabilities(x) - model.predict_proba(x)[:, 1])))
    print(f"numpy 推論と sklearn の最大差 {diff:.6f}")
    if diff > 1e-4:
        print("⚠ 一致しない — 書き出さない", file=sys.stderr)
        return 1
    if args.dry_run:
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    print(f"書込: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
