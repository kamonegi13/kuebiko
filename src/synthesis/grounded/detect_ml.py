"""detect の ML 化 — 学習済みロジスティック回帰で「新規追跡を開く価値」を採点する (2026-09-17)。

設計 (docs/research/llm_training/SYNTHESIS.md §47): 審判ラベル (盲検・現行非依存) を目標に
`detect_features.feature_vector` の 39 列で学習。時系列分割で同量 precision 0.69 (現行 0.38)。
本番はまず **shadow** (ML の選抜を `detect_ml_shadow` に記録するだけ、開設は現行のまま)。

モデルは JSON (``config/models/detect_model.json``、`scripts/train_detect_ml.py` が書く)。
本番は sklearn に依存しない (pair_model と同じ方針)。列の対応は ``feature_names`` の一致で
関門する (列ずれは load で None = ML を黙って使わない)。
"""

from __future__ import annotations

import json
import math
import os
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from src.cti.source_basis import classify_source_tier
from src.synthesis.grounded.detect_features import FEATURE_NAMES, DetectArticle, feature_vector

_log = structlog.get_logger(__name__)

DEFAULT_MODEL_PATH = Path("config/models/detect_model.json")
_SHADOW_ENV = "DETECT_ML_SHADOW"
#: shadow で記録する上限 (下流の消化能力 ≈6 開設/日 に合わせる)
SHADOW_TOP_K = 6
#: 1 run で種別を新たに分類する上限 (fast ティア ~1s/件、synthesis の timeout 内に収める)
_KIND_CLASSIFY_MAX = 200
_CHUNK = 200


@dataclass(frozen=True)
class DetectModel:
    """標準化 + ロジスティック回帰。``feature_names`` の順で値を渡すこと。"""

    feature_names: tuple[str, ...]
    mean: tuple[float, ...]
    scale: tuple[float, ...]
    coef: tuple[float, ...]
    intercept: float
    threshold: float

    def probability(self, features: Sequence[float]) -> float:
        if len(features) != len(self.feature_names):
            raise ValueError(
                f"特徴量の数が合いません: {len(features)} != {len(self.feature_names)}"
            )
        z = self.intercept
        for x, m, s, w in zip(features, self.mean, self.scale, self.coef, strict=True):
            z += w * ((x - m) / s if s else 0.0)
        return 1.0 / (1.0 + math.exp(-z))


def load_detect_model(path: Path | None = None) -> DetectModel | None:
    """JSON を読む。無い / 列が現在の FEATURE_NAMES と違う → None (ML を使わない)。"""
    target = path or DEFAULT_MODEL_PATH
    if not target.exists():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        names = tuple(str(n) for n in raw["feature_names"])
        if names != FEATURE_NAMES:
            _log.warning("detect_ml_feature_mismatch", path=str(target))
            return None
        return DetectModel(
            feature_names=names,
            mean=tuple(float(v) for v in raw["mean"]),
            scale=tuple(float(v) for v in raw["scale"]),
            coef=tuple(float(v) for v in raw["coef"]),
            intercept=float(raw["intercept"]),
            threshold=float(raw["threshold"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        _log.warning("detect_ml_model_invalid", path=str(target), error=type(exc).__name__)
        return None


def shadow_enabled() -> bool:
    """既定 ON。``DETECT_ML_SHADOW=0`` で記録を止める (開設の挙動は元から変えない)。"""
    return os.environ.get(_SHADOW_ENV, "1") != "0"


def shadow_select(
    scores: dict[str, float], *, threshold: float, top_k: int = SHADOW_TOP_K
) -> list[tuple[str, float]]:
    """閾値以上を確率順に最大 top_k 件 (純粋関数)。"""
    ranked = sorted(
        ((a, p) for a, p in scores.items() if p >= threshold), key=lambda t: (-t[1], t[0])
    )
    return ranked[:top_k]


def build_detect_articles(
    repo: Any, article_ids: Sequence[str], kinds: dict[str, str]
) -> dict[str, DetectArticle]:
    """DB 行 + entity 件数 + 種別 → DetectArticle (学習ハーネスと本番で同じ組み立て)。"""
    out: dict[str, DetectArticle] = {}
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    ids = list(dict.fromkeys(article_ids))
    with repo._connect() as conn:  # noqa: SLF001 — 読み取り専用の接続 seam 共有
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


async def ensure_kinds(
    repo: Any,
    articles: Sequence[tuple[str, str, str]],
    classify: Callable[[str, str], Awaitable[str]],
    *,
    model_label: str,
    limit: int = _KIND_CLASSIFY_MAX,
) -> dict[str, str]:
    """種別キャッシュを引き、無いものは classify して cache (上限あり、超過分は other)。"""
    ids = [a[0] for a in articles]
    kinds: dict[str, str] = dict(repo.get_article_kinds(ids))
    missing = [a for a in articles if a[0] not in kinds][:limit]
    for aid, title, summary in missing:
        kind = await classify(title, summary)
        kinds[aid] = kind
        repo.set_article_kind(aid, kind, model_label)
    if missing:
        _log.info("detect_ml_kinds_classified", articles=len(missing))
    return kinds


def score_articles(model: DetectModel, articles: dict[str, DetectArticle]) -> dict[str, float]:
    return {aid: model.probability(feature_vector(a)) for aid, a in articles.items()}
