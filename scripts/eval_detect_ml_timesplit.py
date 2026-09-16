#!/usr/bin/env python3
"""detect ML の評価ハーネス (時系列分割 + 交差検証) — 特徴量は本番と同じ seam を通す。

``src/synthesis/grounded/detect_features.py`` の ``feature_vector`` で DB 行から特徴量を作り、
審判ラベル (``data/mlx/detect_goldset.jsonl``) を目標に、
(a) 時系列分割 (ラベルの run_at 順で前 60% 学習 / 後 40% 評価) と
(b) 層化 5-fold × 3 seed の交差検証 を出す。種別の関門 (kind ∈ {breach, exploitation}) を
決定論のベースラインとして併記する。

sklearn は本番 image に無いので **ホストで DATABASE_URL を本番 PG に向けて** 走らせる
(DATABASE_URL 無しだと空 SQLite へ落ちて 0 件で止まる — rc=1 で検出):
    DATABASE_URL="postgresql://kuebiko:${POSTGRES_PASSWORD:-cti_local_dev}@127.0.0.1:5433/kuebiko" \
        uv run python scripts/eval_detect_ml_timesplit.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
from sklearn.ensemble import RandomForestClassifier  # type: ignore[import-untyped]  # noqa: E402
from sklearn.linear_model import LogisticRegression  # type: ignore[import-untyped]  # noqa: E402
from sklearn.metrics import (  # type: ignore[import-untyped]  # noqa: E402
    average_precision_score,
    roc_auc_score,
)
from sklearn.model_selection import (  # type: ignore[import-untyped]  # noqa: E402
    StratifiedKFold,
    cross_val_predict,
)
from sklearn.pipeline import make_pipeline  # type: ignore[import-untyped]  # noqa: E402
from sklearn.preprocessing import StandardScaler  # type: ignore[import-untyped]  # noqa: E402

from src.cti.source_basis import classify_source_tier  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.synthesis.grounded.detect_features import (  # noqa: E402
    FEATURE_NAMES,
    DetectArticle,
    feature_vector,
)

D = Path("data/mlx")
GOLD = D / "detect_goldset.jsonl"
LABELS = D / "detect_labels.json"
KIND_FILES = (D / "detect_gold_kinds.jsonl", D / "detect_heldout_kinds.jsonl")
GATE_KINDS = frozenset({"breach", "exploitation"})
TRAIN_RATIO = 0.6
SEEDS = 3
_CHUNK = 200


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def load_articles(
    repo: RunHistoryRepository, ids: list[str], kinds: dict[str, str]
) -> dict[str, DetectArticle]:
    out: dict[str, DetectArticle] = {}
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    with repo._connect() as conn:  # noqa: SLF001 — 評価スクリプトの接続 seam 共有
        for i in range(0, len(ids), _CHUNK):
            chunk = ids[i : i + _CHUNK]
            ph = ",".join("?" * len(chunk))
            for r in conn.execute(
                "SELECT article_id, entity_type FROM article_entities "  # noqa: S608
                f"WHERE article_id IN ({ph})",
                tuple(chunk),
            ).fetchall():
                counts[str(r["article_id"])][str(r["entity_type"])] += 1
            for r in conn.execute(
                "SELECT article_id, title, summary, importance, category, feed_title, feed_url, "  # noqa: S608
                f"victim_country_iso, posted_channel FROM articles WHERE article_id IN ({ph})",
                tuple(chunk),
            ).fetchall():
                aid = str(r["article_id"])
                out[aid] = DetectArticle(
                    article_id=aid,
                    title=str(r["title"] or ""),
                    summary=str(r["summary"] or ""),
                    importance=str(r["importance"] or ""),
                    category=str(r["category"] or ""),
                    tier=classify_source_tier(str(r["feed_title"] or ""), str(r["feed_url"] or "")),
                    kind=kinds.get(aid, "other"),
                    victim_country_iso=r["victim_country_iso"],
                    posted_channel=r["posted_channel"],
                    entity_counts=dict(counts.get(aid, {})),
                )
    return out


def _models(seed: int) -> dict[str, Any]:
    return {
        "logreg": make_pipeline(
            StandardScaler(), LogisticRegression(C=0.3, max_iter=3000, class_weight="balanced")
        ),
        "rf": RandomForestClassifier(
            300, min_samples_leaf=3, class_weight="balanced", random_state=seed, n_jobs=4
        ),
    }


def _score(y: np.ndarray, p: np.ndarray, k: int) -> tuple[float, float, float, float]:
    top = np.argsort(-p)[:k]
    return (
        float(roc_auc_score(y, p)),
        float(average_precision_score(y, p)),
        float(y[top].mean()),
        float(y[top].sum() / max(1, y.sum())),
    )


def _fmt(name: str, s: tuple[float, float, float, float], k: int) -> str:
    return (
        f"  {name:<10s} AUC {s[0]:.2f}  AP {s[1]:.2f}  "
        f"同量 {k}: precision {s[2]:.2f} recall {s[3]:.2f}"
    )


def main() -> int:
    gold = {str(g["article_id"]): g for g in _jsonl(GOLD)}
    kinds = {str(r["article_id"]): str(r["kind"]) for f in KIND_FILES for r in _jsonl(f)}
    labels = {str(r["article_id"]): r for r in json.loads(LABELS.read_text(encoding="utf-8"))}
    arts = load_articles(RunHistoryRepository(), sorted(gold), kinds)
    ids = sorted(
        (a for a in gold if a in arts and a in labels), key=lambda a: str(labels[a]["run_at"])
    )
    if not ids:
        print("⚠ 記事が 0 件 — ホスト実行 (空 SQLite) を疑う", file=sys.stderr)
        return 1
    x = np.array([feature_vector(arts[a]) for a in ids], dtype=float)
    opened = np.array([labels[a]["decision"] == "opened" for a in ids])
    k = int(opened.sum())
    print(
        f"n {len(ids)} / 特徴量 {len(FEATURE_NAMES)} / 現行 opened {k} / "
        f"kind 付与 {sum(a in kinds for a in ids)}"
    )
    targets = {
        "gold_open": np.array([int(bool(gold[a]["gold_open"])) for a in ids]),
        "trackable>=2": np.array([int(int(gold[a]["trackable"]) >= 2) for a in ids]),
    }
    n_train = int(len(ids) * TRAIN_RATIO)
    for tname, y in targets.items():
        print(
            f"\n== {tname} (正 {int(y.sum())}) — 現行 detect: {int((opened & (y == 1)).sum())}/{k}"
        )
        gate = np.array([float(arts[a].kind in GATE_KINDS) for a in ids])
        print(
            f"  種別関門 {{breach,exploitation}}: 候補 {int(gate.sum())} 件, "
            f"precision {float(y[gate == 1].mean()):.2f} "
            f"recall {float(y[gate == 1].sum() / y.sum()):.2f}"
        )
        print(
            f"  [時系列分割] 学習 {n_train} (正 {int(y[:n_train].sum())}) / "
            f"評価 {len(ids) - n_train} (正 {int(y[n_train:].sum())}), "
            f"同量 = 評価期間の現行開設 {int(opened[n_train:].sum())}"
        )
        k_test = int(opened[n_train:].sum())
        hit_test = int((opened[n_train:] & (y[n_train:] == 1)).sum())
        print(
            f"  現行 detect (評価期間): {hit_test}/{k_test} = "
            f"precision {hit_test / max(1, k_test):.2f}"
        )
        for mname in ("logreg", "rf"):
            ss = []
            for seed in range(SEEDS):
                m = _models(seed)[mname].fit(x[:n_train], y[:n_train])
                ss.append(_score(y[n_train:], m.predict_proba(x[n_train:])[:, 1], k_test))
            print(_fmt(mname, tuple(float(np.mean(v)) for v in zip(*ss, strict=True)), k_test))  # type: ignore[arg-type]
        print("  [交差検証 5-fold × 3 seed]")
        for mname in ("logreg", "rf"):
            ss = []
            for seed in range(SEEDS):
                p = cross_val_predict(
                    _models(seed)[mname],
                    x,
                    y,
                    cv=StratifiedKFold(5, shuffle=True, random_state=seed),
                    method="predict_proba",
                )[:, 1]
                ss.append(_score(y, p, k))
            print(_fmt(mname, tuple(float(np.mean(v)) for v in zip(*ss, strict=True)), k))  # type: ignore[arg-type]
    # 特徴量の寄与 (RF、全データ、gold_open)
    m = _models(0)["rf"].fit(x, targets["gold_open"])
    top = sorted(zip(FEATURE_NAMES, m.feature_importances_, strict=True), key=lambda t: -t[1])[:12]
    print("\n寄与上位 (RF / gold_open): " + ", ".join(f"{n} {v:.2f}" for n, v in top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
