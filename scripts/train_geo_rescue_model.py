# ruff: noqa: E501  (日本語の文は幅 2 で数えられ長く見える)
"""地政学の救済の学習版 (GEO_RESCUE_LEARNED) の LR を学習し ``config/models/geo_rescue_model.json`` に出す。

    uv run --with scikit-learn==1.9.1 python scripts/train_geo_rescue_model.py \\
        --eval-dir <data/eval/geo_rescue_shadow_2026_11_15> [--out config/models/geo_rescue_model.json]

材料 (eval-dir): ``s26r_replay/replay_s26r.jsonl`` (正解 gold・層・pir/flat・手掛かり・問いの確率)・
``s26r_replay/qall_s26r.jsonl`` (手掛かりのある全行の問いの確率の補完)・``frame_raw.jsonl`` (入力)・
``frame_report.json`` (層の大きさ = 逆確率重み)。埋め込みは Ollama (既定は ``--ollama-url`` の
snowflake-arctic-embed2) で取り ``--embed-cache`` (npz) に保存して再実行を速くする。

方式は eval-dir の ``s26r_replay/learned.py`` の (c) と同じ: [埋め込み | 特徴量 ÷ scale] の LR
(C=0.5、class_weight=balanced、層の重み)・5 分割 × 5 回の交差検証・閾値は学習側で「重み付き誤救済 ≤ 12.1%
(26b の水準) の中で回収最大」を fold ごとに選ぶ。出力する閾値は fold の閾値の中央値。交差検証の成績
(回収・誤救済) は fold の閾値で held-out を判定した値。特徴量は ``src/pipeline/geo_rescue_learned`` と共有
(学習と推論で同じ関数)。推論側は numpy だけで動く (このスクリプトだけが sklearn を使う)。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.pipeline.geo_rescue_learned import (  # noqa: E402
    FEATURE_NAMES,
    MODEL_VERSION,
    build_features,
    embedding_input,
)

FALSE_RESCUE_CAP = 0.121  # 26b の誤救済 (重み付き)。閾値はこの範囲で回収最大
C_REG = 0.5
N_SPLITS = 5
N_REPEATS = 5
PROBE_MODEL = "kuebiko-sft:s26r"
DEFAULT_EMBED_MODEL = "snowflake-arctic-embed2"
THRESHOLD_GRID = np.linspace(0.05, 0.95, 37)


def _load(p: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def _features(r: dict[str, Any], q: dict[str, Any]) -> tuple[float, ...]:
    return build_features(
        pir=r["pir"],
        flat=r["flat"],
        cues=r["cues"],
        p_qw=q.get("qw"),
        p_qc=q.get("qc"),
        p_qd=q.get("qd"),
        is_summary=r.get("input_kind") == "summary",
    )


async def _embed(texts: list[str], url: str, model: str) -> np.ndarray:
    from src.tools.embedding_client import OllamaEmbeddingClient

    cli = OllamaEmbeddingClient(base_url=url, model=model)
    out = [(await cli.embed(t, kind="document")).vector for t in texts]
    return np.array(out, dtype=np.float32)


def _rates(y: np.ndarray, yes: np.ndarray, w: np.ndarray) -> tuple[float, float]:
    pos, neg = y == 1, y == 0
    return (
        float((w * (yes & pos)).sum() / (w * pos).sum()),
        float((w * (yes & neg)).sum() / (w * neg).sum()),
    )


def _pick_threshold(p: np.ndarray, y: np.ndarray, w: np.ndarray) -> float:
    best, best_recall = 0.5, -1.0
    for t in THRESHOLD_GRID:
        recall, fp = _rates(y, p >= t, w)
        if fp <= FALSE_RESCUE_CAP and recall > best_recall:
            best, best_recall = float(t), recall
    return best


def _fit(x: np.ndarray, y: np.ndarray, w: np.ndarray) -> Any:
    from sklearn.linear_model import LogisticRegression

    return LogisticRegression(C=C_REG, max_iter=2000, class_weight="balanced").fit(
        x, y, sample_weight=w
    )


def cross_validate(x: np.ndarray, y: np.ndarray, w: np.ndarray) -> dict[str, Any]:
    from sklearn.model_selection import StratifiedKFold

    votes = np.zeros(len(y))
    thresholds: list[float] = []
    for rep in range(N_REPEATS):
        skf = StratifiedKFold(N_SPLITS, shuffle=True, random_state=rep)
        for tr, te in skf.split(x, y):
            m = _fit(x[tr], y[tr], w[tr])
            t = _pick_threshold(m.predict_proba(x[tr])[:, 1], y[tr], w[tr])
            thresholds.append(t)
            votes[te] += m.predict_proba(x[te])[:, 1] >= t
    recall, fp = _rates(y, votes / N_REPEATS >= 0.5, w)
    return {"recall": recall, "false_rescue": fp, "fold_thresholds": thresholds}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--eval-dir", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path("config/models/geo_rescue_model.json"))
    ap.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    ap.add_argument("--embed-model", default=DEFAULT_EMBED_MODEL)
    ap.add_argument("--embed-cache", type=Path, default=None)
    a = ap.parse_args()
    d: Path = a.eval_dir
    rows = _load(d / "s26r_replay/replay_s26r.jsonl")
    qall = {r["key"]: r for r in _load(d / "s26r_replay/qall_s26r.jsonl")}
    frame = {r["article_id"]: r for r in _load(d / "frame_raw.jsonl")}
    sizes = json.loads((d / "frame_report.json").read_text())["stratum_size"]

    y = np.array([int(r["gold"]) for r in rows])
    n_by = {s: sum(r["stratum"] == s for r in rows) for s in ("Y", "N")}
    w = np.array([sizes[r["stratum"]] / n_by[r["stratum"]] for r in rows])
    xf = np.array([_features(r, qall.get(r["key"], r)) for r in rows], dtype=np.float64)
    scale = np.maximum(xf.max(axis=0), 1.0)

    texts = [
        embedding_input(frame[r["article_id"]]["title"], frame[r["article_id"]]["input_text"])
        for r in rows
    ]
    if a.embed_cache and a.embed_cache.exists():
        emb = np.load(a.embed_cache)["emb"]
    else:
        emb = asyncio.run(_embed(texts, a.ollama_url, a.embed_model))
        if a.embed_cache:
            np.savez(a.embed_cache, emb=emb)
    x = np.hstack([emb, xf / scale])

    cv = cross_validate(x, y, w)
    threshold = float(np.median(cv["fold_thresholds"]))
    model = _fit(x, y, w)
    out = {
        "version": MODEL_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "scale": [float(s) for s in scale],
        "embedding_dim": int(emb.shape[1]),
        "coef": [float(c) for c in model.coef_[0]],
        "intercept": float(model.intercept_[0]),
        "threshold": threshold,
        "threshold_method": (
            f"fold ごとに学習側で重み付き誤救済 <= {FALSE_RESCUE_CAP} の範囲の回収最大の閾値を選び、"
            "その中央値 (learned.py と同じ決め方)"
        ),
        "probe_model": PROBE_MODEL,
        "embedding_model": a.embed_model,
        "train_rows": len(rows),
        "train_gold_yes": int(y.sum()),
        "regularization_C": C_REG,
        "cv": {
            "recall": round(cv["recall"], 4),
            "false_rescue": round(cv["false_rescue"], 4),
            "folds": f"{N_SPLITS} 分割 x {N_REPEATS} 回 (多数決)",
            "threshold_min": float(min(cv["fold_thresholds"])),
            "threshold_max": float(max(cv["fold_thresholds"])),
        },
    }
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(
        f"CV 回収 {cv['recall']:.3f} 誤救済 {cv['false_rescue'] * 100:.1f}% "
        f"閾値(中央値) {threshold:.3f} -> {a.out}"
    )


if __name__ == "__main__":
    main()
