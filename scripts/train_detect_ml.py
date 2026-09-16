#!/usr/bin/env python3
"""detect ML (ロジスティック回帰) を審判ラベルで学習し JSON に書き出す (2026-09-17)。

入力: data/mlx/detect_goldset.jsonl (審判) + data/mlx/detect_gold_kinds.jsonl /
detect_heldout_kinds.jsonl (種別) + DB (記事行・entity)。特徴量は本番と同じ
``detect_features.feature_vector``。出力: config/models/detect_model.json
(mean / scale / coef / intercept / threshold / feature_names)。

閾値は「held-out 期間に ML が正と言う件数が 1 日あたり ``--target-per-day`` 件」になる分位で
決める (下流の消化能力から逆算、SYNTHESIS §47)。

sklearn は本番 image に無いので **ホストで DATABASE_URL を本番 PG に向けて** 走らせる
(eval_detect_ml_timesplit.py と同じ。無しだと空 SQLite → rc=1):
    uv run python scripts/train_detect_ml.py [--target-per-day 6] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
from sklearn.linear_model import LogisticRegression  # type: ignore[import-untyped]  # noqa: E402
from sklearn.preprocessing import StandardScaler  # type: ignore[import-untyped]  # noqa: E402

from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.synthesis.grounded.detect_features import FEATURE_NAMES, feature_vector  # noqa: E402
from src.synthesis.grounded.detect_ml import DEFAULT_MODEL_PATH, build_detect_articles  # noqa: E402

D = Path("data/mlx")
GOLD = D / "detect_goldset.jsonl"
LABELS = D / "detect_labels.json"
KIND_FILES = (D / "detect_gold_kinds.jsonl", D / "detect_heldout_kinds.jsonl")
_C = 0.3
_HELD_OUT_RATIO = 0.7


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def choose_threshold(probs: np.ndarray, *, days: int, target_per_day: float) -> float:
    """held-out 全記事の確率から、1 日 target 件が閾値以上になる分位を返す (純粋関数)。"""
    want = max(1, int(round(days * target_per_day)))
    if want >= len(probs):
        return float(probs.min())
    return float(np.sort(probs)[::-1][want - 1])


def export(scaler: Any, clf: Any, threshold: float) -> dict[str, Any]:
    return {
        "feature_names": list(FEATURE_NAMES),
        "mean": [float(v) for v in scaler.mean_],
        "scale": [float(v) for v in scaler.scale_],
        "coef": [float(v) for v in clf.coef_[0]],
        "intercept": float(clf.intercept_[0]),
        "threshold": threshold,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target-per-day", type=float, default=6.0)
    ap.add_argument("--out", type=Path, default=DEFAULT_MODEL_PATH)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    gold = {str(g["article_id"]): g for g in _jsonl(GOLD)}
    kinds = {str(r["article_id"]): str(r["kind"]) for f in KIND_FILES for r in _jsonl(f)}
    labels: list[dict[str, Any]] = json.loads(LABELS.read_text(encoding="utf-8"))
    labels.sort(key=lambda r: str(r["run_at"]))
    held_out = labels[int(len(labels) * _HELD_OUT_RATIO) :]
    repo = RunHistoryRepository()
    train_ids = sorted(gold)
    arts = build_detect_articles(repo, train_ids, kinds)
    ids = [a for a in train_ids if a in arts]
    if not ids:
        print("⚠ 記事 0 件 — DATABASE_URL 無し (空 SQLite) を疑う", file=sys.stderr)
        return 1
    x = np.array([feature_vector(arts[a]) for a in ids], dtype=float)
    y = np.array([int(bool(gold[a]["gold_open"])) for a in ids])
    scaler = StandardScaler().fit(x)
    clf = LogisticRegression(C=_C, max_iter=3000, class_weight="balanced").fit(
        scaler.transform(x), y
    )
    # 閾値: held-out 全記事 (審判外も含む) の確率分布から消化能力の分位で決める
    ho_ids = [str(r["article_id"]) for r in held_out]
    ho_arts = build_detect_articles(repo, ho_ids, kinds)
    ho_x = np.array([feature_vector(ho_arts[a]) for a in ho_ids if a in ho_arts], dtype=float)
    ho_p = clf.predict_proba(scaler.transform(ho_x))[:, 1]
    first = date.fromisoformat(str(held_out[0]["run_at"])[:10])
    last = date.fromisoformat(str(held_out[-1]["run_at"])[:10])
    days = (last - first).days + 1
    threshold = choose_threshold(ho_p, days=days, target_per_day=args.target_per_day)
    picked = int((ho_p >= threshold).sum())
    train_p = clf.predict_proba(scaler.transform(x))[:, 1]
    print(
        f"学習 {len(ids)} 件 (正 {int(y.sum())}) / held-out {len(ho_x)} 件 {days} 日 → "
        f"閾値 {threshold:.3f} で {picked} 件 ({picked / days:.1f}/日) / "
        f"学習集合で閾値以上 {int((train_p >= threshold).sum())} 件 うち正 "
        f"{int(((train_p >= threshold) & (y == 1)).sum())}"
    )
    top = sorted(zip(FEATURE_NAMES, clf.coef_[0], strict=True), key=lambda t: -abs(t[1]))[:10]
    print("係数 (標準化後) 上位: " + ", ".join(f"{n} {v:+.2f}" for n, v in top))
    if args.dry_run:
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(export(scaler, clf, threshold), ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    print(f"書込: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
