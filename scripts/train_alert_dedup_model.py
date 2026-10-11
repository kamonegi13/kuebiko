#!/usr/bin/env python3
"""重複 alert の ML を学習し、木を JSON に書き出す (R1h、2026-10-11)。

学習データ = Opus 正解の alert の組 (data/eval/r1g_gold_v1.jsonl、目的変数 = suppress、
boundary・realert は 0)。特徴量は **本番と同じ関数** (``src.cti.alert_dedup_features``) を
as-of (今回の配信時点の情報だけ) で呼んで作る — train/serve の一致。
先行は gold の先行を使うが、本番の先行の選び方 (cos >= 0.70 + pair_model、72h) に
**当たらない組は学習から除く** (本番では記録されない組のため)。

sklearn は本番 image に無いので **ホストで DATABASE_URL を本番 PG に向けて** 走らせる:
    DATABASE_URL=postgresql://kuebiko:${POSTGRES_PASSWORD}@127.0.0.1:5433/kuebiko \\
        uv run --with scikit-learn python scripts/train_alert_dedup_model.py
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
from sklearn.ensemble import (  # type: ignore[import-untyped]  # noqa: E402
    ExtraTreesClassifier,
    RandomForestClassifier,
)
from sklearn.metrics import roc_auc_score  # type: ignore[import-untyped]  # noqa: E402
from sklearn.model_selection import StratifiedKFold  # type: ignore[import-untyped]  # noqa: E402

from src.cti.alert_dedup_features import (  # noqa: E402
    FEATURE_NAMES,
    AlertSide,
    alert_dedup_features,
    select_prior,
)
from src.cti.alert_dedup_model import DEFAULT_MODEL_PATH, DEFAULT_THRESHOLD  # noqa: E402
from src.eventnews.pair_model import load_model  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.tools.url_normalizer import url_hash  # noqa: E402

GOLD = Path("data/eval/r1g_gold_v1.jsonl")
CANDIDATES = Path("data/eval/r1g_candidates_v1.json")
N_TREES = 300
MAX_DEPTH = 4
MIN_LEAF = 5
CV_FOLDS = 5
SEED = 0


def _aware(raw: object) -> datetime:
    dt = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class SideLoader:
    """記事 id から AlertSide を作る (DB 読み取りのみ)。時刻は alert 再現の時刻を優先。"""

    def __init__(self, repo: RunHistoryRepository, alert_at: dict[str, datetime]) -> None:
        self._repo = repo
        self._alert_at = alert_at
        self._cache: dict[str, AlertSide | None] = {}

    def load(self, article_id: str) -> AlertSide | None:
        if article_id in self._cache:
            return self._cache[article_id]
        side = self._build(article_id)
        self._cache[article_id] = side
        return side

    def _build(self, article_id: str) -> AlertSide | None:
        art = self._repo.get_article(article_id)
        if art is None:
            return None
        vec = self._repo.get_embedding(url_hash(art.url))
        if vec is None:
            return None
        return AlertSide(
            article_id=article_id,
            title=art.title or "",
            summary=art.summary or "",
            category=art.category or "",
            feed_title=art.feed_title or "",
            feed_url=art.feed_url or "",
            account_class=art.account_class or "",
            time=self._alert_at.get(article_id) or _aware(art.created_at),
            entities=frozenset(self._repo.get_article_entities(article_id)),
            vector=np.asarray(vec, dtype=np.float32),
        )


def build_xy(
    gold: Path, candidates: Path, repo: RunHistoryRepository
) -> tuple[Any, Any, dict[str, int]]:
    pair_model = load_model()
    if pair_model is None:
        raise SystemExit("pair_model が読めない")
    alert_at = {
        a: _aware(t)
        for a, t in json.loads(candidates.read_text(encoding="utf-8"))["alert_ids"].items()
    }
    loader = SideLoader(repo, alert_at)
    cand_sides = [s for s in (loader.load(a) for a in alert_at) if s is not None]
    stats = {"gold": 0, "no_data": 0, "prior_not_found": 0, "used": 0, "newest_is_gold_prior": 0}
    xs: list[list[float]] = []
    ys: list[int] = []
    for line in gold.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        stats["gold"] += 1
        cur, prior = loader.load(row["cur_id"]), loader.load(row["prior_id"])
        if cur is None or prior is None:
            stats["no_data"] += 1
            continue
        hit = select_prior(cur, [prior], pair_model)
        if hit is None:
            stats["prior_not_found"] += 1
            continue
        newest = select_prior(cur, cand_sides, pair_model)
        stats["newest_is_gold_prior"] += int(
            newest is not None and newest.side.article_id == prior.article_id
        )
        xs.append(alert_dedup_features(cur, prior, hit.p_pair))
        ys.append(int(row["verdict"] == "suppress"))
        stats["used"] += 1
    return np.array(xs, dtype=np.float32), np.array(ys), stats


def make_model(kind: str) -> Any:
    cls = ExtraTreesClassifier if kind == "extra_trees" else RandomForestClassifier
    return cls(
        N_TREES,
        max_depth=MAX_DEPTH,
        min_samples_leaf=MIN_LEAF,
        class_weight="balanced",
        random_state=SEED,
    )


def cv_auc(kind: str, x: Any, y: Any) -> float:
    p = np.zeros(len(y))
    for tr, te in StratifiedKFold(CV_FOLDS, shuffle=True, random_state=SEED).split(x, y):
        p[te] = make_model(kind).fit(x[tr], y[tr]).predict_proba(x[te])[:, 1]
    return float(roc_auc_score(y, p))


def export_trees(model: Any) -> list[dict[str, list[float]]]:
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
                "threshold": [float(v) for v in t.threshold],  # ⚠ 丸めない
                "proba": [float(v) for v in p1],
            }
        )
    return trees


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gold", type=Path, default=GOLD)
    ap.add_argument("--candidates", type=Path, default=CANDIDATES)
    ap.add_argument("--out", type=Path, default=DEFAULT_MODEL_PATH)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    x, y, stats = build_xy(args.gold, args.candidates, RunHistoryRepository())
    print("件数", json.dumps(stats, ensure_ascii=False))
    if len(y) == 0 or len(set(y.tolist())) < 2:
        print("⚠ 学習できる組が無い — DATABASE_URL 無し (空 SQLite) を疑う", file=sys.stderr)
        return 1
    print(f"学習 {len(y)} 組 / suppress {int(y.sum())}")
    scores = {k: cv_auc(k, x, y) for k in ("extra_trees", "random_forest")}
    print("5 分割 CV AUC", json.dumps({k: round(v, 3) for k, v in scores.items()}))
    kind = max(scores, key=lambda k: scores[k])
    model = make_model(kind).fit(x, y)
    now = datetime.now(UTC)
    out = {
        "version": f"alert_dedup_v1_{now:%Y%m%d}",
        "trained_at": now.isoformat(timespec="seconds"),
        "n_samples": int(len(y)),
        "n_suppress": int(y.sum()),
        "model_kind": kind,
        "cv_auc": round(scores[kind], 4),
        "cv_auc_all": {k: round(v, 4) for k, v in scores.items()},
        "threshold": DEFAULT_THRESHOLD,
        "feature_names": list(FEATURE_NAMES),
        "trees": export_trees(model),
    }
    if args.dry_run:
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    print(f"書込: {args.out}")
    from src.cti.alert_dedup_model import load_alert_dedup_model

    loaded = load_alert_dedup_model(str(args.out))
    if loaded is None:
        print("⚠ 書いたモデルが読めない", file=sys.stderr)
        return 1
    diff = max(
        abs(
            loaded.p_suppress(list(map(float, r)))
            - float(model.predict_proba(r.reshape(1, -1))[0, 1])
        )
        for r in x
    )
    print(f"numpy 推論と sklearn の最大差 {diff:.6f}")
    return 0 if diff < 1e-4 else 1


if __name__ == "__main__":
    raise SystemExit(main())
