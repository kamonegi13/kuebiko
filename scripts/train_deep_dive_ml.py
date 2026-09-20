#!/usr/bin/env python3
"""深掘り選定の蒸留モデルを学習する (2026-09-20)。

**(A) 蒸留** — LLM rubric の composite を回帰で再現する。狙いは 2 つ:

1. **関門 A の置き換え** (約 950 → 60)。現行は整数 5 項目の決定論 composite で、
   **単独報道の high が構造的に落ちる** (60+8=68 点 < 切り口 78-88)。実測で high の
   67% が LLM に一度も届いていなかった。ML なら記事の内容から推定できる。
2. **関門 B の高速化** (60 → 20)。12k トークン 5 分の呼出を数秒にする。

⚠ **これは現行 (LLM rubric) を真値に置く設計**。品質の改善は狙っていない。
軌道に乗ったら (B) 審判ラベルで測り直すこと。detect ML も現行由来ラベルで一度棄却され、
審判ラベルに変えたら AUC 0.84 が出た。

⚠ 教師は**直近の窓に限る**。抽出の改良で古い窓ほど特徴が薄い (記事あたり entity が
09-14 で 8.2、06-15 で 3.3)。混ぜると 2 つの制度を学ぶ。

モデルは JSON (sklearn 非依存、detect_model と同じ方針)。列の対応は feature_names で関門。

    DATABASE_URL=... uv run python scripts/train_deep_dive_ml.py \\
        --labels data/mlx/deep_dive_labels.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from sklearn.linear_model import Ridge  # type: ignore[import-untyped]  # noqa: E402
from sklearn.preprocessing import StandardScaler  # type: ignore[import-untyped]  # noqa: E402

from src.digest.deep_dive_features import (  # noqa: E402
    DD_FEATURE_NAMES,
    DeepDiveExtra,
    dd_feature_vector,
)
from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.synthesis.grounded.detect_ml import build_detect_articles  # noqa: E402

DEFAULT_MODEL_PATH = Path("config/models/deep_dive_model.json")
_ALPHA = 1.0


def load_labels(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    # ⚠ 採点されなかった候補は落とす。0 を代入すると「最低評価」という別の教師になる。
    return [r for r in rows if r.get("composite") is not None]


def build_matrix(
    rows: list[dict[str, Any]], repo: RunHistoryRepository
) -> tuple[np.ndarray, np.ndarray, list[dict[str, Any]]]:
    ids = [r["article_id"] for r in rows]
    arts = build_detect_articles(repo, ids, {})
    meta = _fetch_meta(repo, ids)
    xs: list[list[float]] = []
    ys: list[float] = []
    kept: list[dict[str, Any]] = []
    for r in rows:
        a = arts.get(r["article_id"])
        if a is None:
            continue
        m = meta.get(r["article_id"], {})
        xs.append(
            dd_feature_vector(
                a,
                DeepDiveExtra(
                    corroboration=int(m.get("corroboration", 1)),
                    age_hours=float(m.get("age_hours", 0.0)),
                    is_novel=True,
                    summary_len=int(m.get("summary_len", 0)),
                ),
            )
        )
        ys.append(float(r["composite"]))
        kept.append(r)
    return np.asarray(xs, dtype=float), np.asarray(ys, dtype=float), kept


def _fetch_meta(repo: RunHistoryRepository, ids: list[str]) -> dict[str, dict[str, Any]]:
    """要約長と報道の広がりを 1 度に引く (N+1 回避)。"""
    out: dict[str, dict[str, Any]] = {}
    if not ids:
        return out
    uniq = list(dict.fromkeys(ids))
    with repo._connect() as conn:  # noqa: SLF001 — 読み取り専用の接続 seam 共有
        for i in range(0, len(uniq), 400):
            chunk = uniq[i : i + 400]
            ph = ",".join("?" * len(chunk))
            for r in conn.execute(
                "SELECT article_id, dedup_key, length(coalesce(summary,'')) AS n "  # noqa: S608
                f"FROM articles WHERE article_id IN ({ph})",
                tuple(chunk),
            ).fetchall():
                out[str(r["article_id"])] = {
                    "summary_len": int(r["n"] or 0),
                    "dedup_key": r["dedup_key"],
                    "corroboration": 1,
                    "age_hours": 0.0,
                }
    return out


def top_k_agreement(y_true: np.ndarray, y_pred: np.ndarray, *, k: int) -> float:
    """上位 k 件の一致率 — 蒸留の合否はここで見る (回帰誤差ではなく選抜の一致)。"""
    if len(y_true) <= k:
        return 1.0
    a = set(np.argsort(y_true)[::-1][:k].tolist())
    b = set(np.argsort(y_pred)[::-1][:k].tolist())
    return len(a & b) / k


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--labels", default="data/mlx/deep_dive_labels.jsonl")
    ap.add_argument("--out", default=str(DEFAULT_MODEL_PATH))
    ap.add_argument("--holdout-windows", type=int, default=3, help="新しい側を検証に回す窓数")
    args = ap.parse_args()

    rows = load_labels(Path(args.labels))
    if not rows:
        print("教師がありません")
        return 1
    repo = RunHistoryRepository()
    x, y, kept = build_matrix(rows, repo)
    print(f"教師 {len(rows)} 行 → 特徴量が組めた {len(kept)} 行 / 列 {x.shape[1]}")

    # ⭐ **時系列分割**。無作為に割ると同じ窓の記事が両側に入り、成績が水増しされる。
    windows = sorted({r["window_end"] for r in kept}, reverse=True)
    test_w = set(windows[: args.holdout_windows])
    is_test = np.array([r["window_end"] in test_w for r in kept])
    print(f"窓 {len(windows)} 本 — 検証 {len(test_w)} 本 ({sum(is_test)} 行)")

    scaler = StandardScaler().fit(x[~is_test])
    model = Ridge(alpha=_ALPHA).fit(scaler.transform(x[~is_test]), y[~is_test])
    pred = model.predict(scaler.transform(x[is_test]))
    truth = y[is_test]

    # 窓ごとに上位 20 の一致を見る (本番の選抜単位)
    per_window = []
    for w in sorted(test_w):
        idx = [i for i, r in enumerate(kept) if r["window_end"] == w and is_test[i]]
        if not idx:
            continue
        sub = [list(np.where(np.where(is_test)[0] == i)[0]) for i in idx]
        flat = [s[0] for s in sub if s]
        if len(flat) < 20:
            continue
        per_window.append(top_k_agreement(truth[flat], pred[flat], k=20))
    corr = float(np.corrcoef(truth, pred)[0, 1]) if len(truth) > 2 else float("nan")
    print(f"\n検証: 相関 {corr:.3f}")
    if per_window:
        print(f"      上位 20 の一致率 {np.mean(per_window) * 100:.0f}% (窓 {len(per_window)} 本)")
    else:
        print("      上位 20 の一致率: 窓あたりの検証行が足りず測れず")

    out = {
        "feature_names": list(DD_FEATURE_NAMES),
        "mean": [float(v) for v in scaler.mean_],
        "scale": [float(v) for v in scaler.scale_],
        "coef": [float(v) for v in model.coef_],
        "intercept": float(model.intercept_),
        "trained_windows": len(windows) - len(test_w),
        "holdout_corr": corr,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(
        json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(f"\n書込: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
