#!/usr/bin/env python3
"""台帳の割当判定 ML を学習し、木を JSON に書き出す (2026-09-24)。

学習データ = Opus 盲検の組 (data/mlx/assign_goldset.jsonl)。**特徴量は本番の関数
(`assign_features.assign_feature_vector`) で作る** — 学習と本番の計算を食い違わせない。
閾値は情勢で分けた交差検証の確率から、``--min-precision`` を満たす中で回収が最大の点を選ぶ。

sklearn は本番 image に無いので **ホストで DATABASE_URL を本番 PG に向けて** 走らせる。
埋込はホストの Ollama (127.0.0.1) で作る:
    DATABASE_URL=... OLLAMA_BASE_URL=http://127.0.0.1:11434 \\
        uv run python scripts/train_assign_model.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
from sklearn.ensemble import RandomForestClassifier  # type: ignore[import-untyped]  # noqa: E402
from sklearn.model_selection import GroupKFold  # type: ignore[import-untyped]  # noqa: E402

from src.assessment.assign_features import FEATURE_NAMES, assign_feature_vector  # noqa: E402
from src.assessment.assign_model import DEFAULT_MODEL_PATH  # noqa: E402
from src.assessment.assignment import build_article_keys, build_situation_keys  # noqa: E402
from src.assessment.situation_store import SituationStore  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402

GOLD = Path("data/mlx/assign_goldset.jsonl")
N_TREES = 400
MIN_LEAF = 3


async def build_xy(repo: RunHistoryRepository, store: SituationStore) -> tuple[Any, Any, list[str]]:
    from src.assessment.stateful import _days_between, _embed_texts, _seed_centroids, _unit_vectors

    gold = [json.loads(x) for x in GOLD.read_text(encoding="utf-8").splitlines() if x.strip()]
    rows = {r.situation_id: r for r in store.load_situations(("active", "dormant", "closed"))}
    claim = {}
    for sid in rows:
        rev = store.latest_revision(sid)
        claim[sid] = rev.claim_type if rev else ""
    aids = sorted({g["article_id"] for g in gold})
    ents = repo.entity_keys_for_articles(aids)
    arts = repo.get_articles_by_ids(aids)
    art_vecs = _unit_vectors(repo.load_summary_embeddings(aids))
    inputs = repo.summary_embedding_inputs([a for a in aids if a not in art_vecs])
    miss = sorted(inputs)
    art_vecs.update(
        {
            a: v
            for a, v in zip(miss, await _embed_texts([inputs[a] for a in miss]), strict=True)
            if v is not None
        }
    )
    sids = sorted({g["situation_id"] for g in gold if g["situation_id"] in rows})
    sit_vecs = dict(zip(sids, await _embed_texts([rows[s].title for s in sids]), strict=True))
    seeds = _seed_centroids(sids, art_vecs, repo=repo, store=store)
    xs, ys, groups = [], [], []
    for g in gold:
        sid, aid = g["situation_id"], g["article_id"]
        if (
            sid not in rows
            or aid not in arts
            or art_vecs.get(aid) is None
            or sit_vecs.get(sid) is None
        ):
            continue
        ak = build_article_keys(
            article_id=aid, title=arts[aid].title or "", entity_keys=frozenset(ents.get(aid, set()))
        )
        sk = build_situation_keys(rows[sid], claim_type=claim.get(sid, ""))
        av = art_vecs[aid]
        seed = seeds.get(sid)
        xs.append(
            assign_feature_vector(
                ak,
                sk,
                cos_title=float(av @ sit_vecs[sid]),
                cos_seed=None if seed is None else float(av @ seed),
                age_days=_days_between(rows[sid].opened_at, arts[aid].created_at),
            )
        )
        ys.append(int(g["label"] == "same"))
        groups.append(sid)
    return np.array(xs, dtype=np.float32), np.array(ys), groups


def choose_threshold(p: Any, y: Any, *, min_precision: float) -> tuple[float, float, float]:
    """交差検証の確率から、精度が下限以上で回収が最大の閾値 (純粋関数)。"""
    best = (0.5, 0.0, 0.0)
    for t in np.arange(0.30, 0.96, 0.01):
        sel = p >= t
        if not sel.any():
            continue
        prec = float((sel & (y == 1)).sum() / sel.sum())
        rec = float((sel & (y == 1)).sum() / max(1, y.sum()))
        if prec >= min_precision and rec > best[2]:
            best = (float(t), prec, rec)
    return best


def export(model: Any, threshold: float, n: int) -> dict[str, Any]:
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
        "n_samples": n,
        "feature_names": list(FEATURE_NAMES),
        "threshold": threshold,
        "trees": trees,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-precision", type=float, default=0.75)
    ap.add_argument("--out", type=Path, default=DEFAULT_MODEL_PATH)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    repo = RunHistoryRepository()
    x, y, groups = asyncio.run(build_xy(repo, SituationStore()))
    if len(y) == 0:
        print("⚠ 組 0 件 — DATABASE_URL 無し (空 SQLite) を疑う", file=sys.stderr)
        return 1

    def mk() -> Any:
        return RandomForestClassifier(
            N_TREES, min_samples_leaf=MIN_LEAF, class_weight="balanced", random_state=0
        )

    p = np.zeros(len(y))
    for tr, te in GroupKFold(5).split(x, y, groups):
        p[te] = mk().fit(x[tr], y[tr]).predict_proba(x[te])[:, 1]
    # ⭐ 閾値は **本番で判定する組 = 規則が候補に出した組** の中で決める
    #   (審判の組は層化で集めたので、全体の比率は本番と違う)
    ruled = np.zeros(len(y), dtype=bool)
    for r in ("anchor", "nation", "token"):
        ruled |= x[:, FEATURE_NAMES.index(f"rule_{r}")] > 0
    t, prec, rec = choose_threshold(p[ruled], y[ruled], min_precision=args.min_precision)
    print(
        f"組 {len(y)} / 同じ {int(y.sum())} / 情勢 {len(set(groups))}"
        f" / 規則の候補 {int(ruled.sum())}"
    )
    print(f"閾値 {t:.2f} (規則の候補の交差検証: 精度 {100 * prec:.0f}% 回収 {100 * rec:.0f}%)")
    model = mk().fit(x, y)
    out = export(model, t, len(y))
    # 書き出した木を numpy 実装で読み、sklearn と一致するか照合する
    from src.assessment.assign_model import AssignModel

    mine = AssignModel(
        feature_names=tuple(out["feature_names"]), threshold=t, _trees=tuple(out["trees"])
    )
    diff = max(
        abs(mine.probability(list(r)) - s)
        for r, s in zip(x, model.predict_proba(x)[:, 1], strict=True)
    )
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
