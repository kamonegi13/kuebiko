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
import re
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from src.cti.japan_relevance import is_japan_targeted_row
from src.cti.source_basis import classify_source_tier
from src.synthesis.grounded.detect_features import (
    FEATURE_NAMES,
    KNOWN_ACTOR_KEY,
    DetectArticle,
    feature_vector,
)

_log = structlog.get_logger(__name__)

DEFAULT_MODEL_PATH = Path("config/models/detect_model.json")
_SHADOW_ENV = "DETECT_ML_SHADOW"
_PREFILTER_ENV = "DETECT_ML_PREFILTER"
#: detect (LLM) に渡す候補を ML の上位この件数に絞る既定値。**15 → 30** (2026-09-18)。
#: 凍結データ (時系列分割・評価 10 日) で候補セットの取りこぼしを実測した結果:
#:   top15 → 審判=開設の 4 記事 / 3 事象が候補外、top20 → 4 / 3、top25 → 3 / 3、**top30 → 1 / 1**。
#: 確率の下限を足す案 (top20 + p>=0.75 等) は 2 / 2 止まりで、件数を増やす方が効いた
#: (日によって記事量が 3 倍違い、高確率でも上位 20 に入り切らない日がある)。
#: top30 でも候補は中央 33 件 = 従来の全件投入 (150-500) の 5-15 分の 1。0 で無効
PREFILTER_TOP_K_DEFAULT = 30
#: shadow で記録する上限 (下流の消化能力 ≈6 開設/日 に合わせる)
SHADOW_TOP_K = 6
#: 月例更新・カタログ追加・注意喚起の類 = 勧告であって追跡単位でない (2026-09-15「勧告は見張り」)。
#: ML は importance と kind で拾ってしまうため、候補から決定論で外す (バックテスト 09-17)
ROLLUP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"月例|定例|セキュリティ情報公開|セキュリティ更新プログラム"),
    re.compile(r"Patch Tuesday|Security Update Guide", re.IGNORECASE),
    re.compile(r"KEV カタログに追加|KEV に追加|Known Exploited Vulnerabilities"),
    re.compile(r"注意喚起を発信|注意喚起を公開|advisory roundup", re.IGNORECASE),
)
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


def prefilter_top_k() -> int:
    """LLM detect の前段で候補を絞る件数 (``DETECT_ML_PREFILTER``、既定 15、0 = 絞らない)。"""
    raw = os.environ.get(_PREFILTER_ENV, str(PREFILTER_TOP_K_DEFAULT))
    try:
        return max(0, int(raw))
    except ValueError:
        return PREFILTER_TOP_K_DEFAULT


def prefilter_select(scores: dict[str, float], *, top_k: int) -> list[str]:
    """確率順に上位 top_k 件の article_id (純粋関数)。top_k<=0 なら全件をそのまま返す。"""
    ranked = sorted(scores, key=lambda a: (-scores[a], a))
    return ranked if top_k <= 0 else ranked[:top_k]


def floor_article_ids(articles: Mapping[str, DetectArticle]) -> set[str]:
    """ML の順位に関わらず必ず LLM へ渡す記事 (下限保証、2026-09-18)。

    - ``importance=high``: 国家系など特徴量に写らない文脈を LLM に見せる
    - **日本標的の breach**: 被害組織が日本で侵害型のもの。CVE も TTP も持たない国内侵害は
      特徴量が薄く確率が上がらないが (実測: 日本の大学への侵入が p=0.27 で候補外)、
      任務上は追跡単位そのもの。実測の増分は 1 日あたり数件。
    """
    return {
        aid
        for aid, a in articles.items()
        if a.importance == "high"
        or (a.kind == "breach" and is_japan_targeted_row(a.victim_country_iso, a.posted_channel))
    }


def is_rollup_title(title: str) -> bool:
    """月例・カタログ追加・注意喚起の記事か (追跡単位にしない、決定論)。"""
    return any(p.search(title) for p in ROLLUP_PATTERNS)


def compose_llm_candidates(
    scores: dict[str, float],
    *,
    top_k: int,
    high_ids: set[str],
    excluded: set[str],
) -> list[str]:
    """LLM detect に渡す候補 = ML 上位 top_k ∪ 下限保証 (``floor_article_ids``) − 除外 (純粋関数)。

    下限保証は、特徴量に写らない文脈 (国家系・CVE を持たない国内侵害) を LLM に見せるため。
    scores が空 (モデル無し) でも除外だけは効く (呼び出し側が全候補を渡す)。
    """
    eligible = {a: p for a, p in scores.items() if a not in excluded}
    ordered = prefilter_select(eligible, top_k=top_k)
    extra = sorted(a for a in high_ids if a not in excluded and a not in ordered)
    return ordered + extra


def shadow_select(
    scores: dict[str, float], *, threshold: float, top_k: int = SHADOW_TOP_K
) -> list[tuple[str, float]]:
    """閾値以上を確率順に最大 top_k 件 (純粋関数)。"""
    ranked = sorted(
        ((a, p) for a, p in scores.items() if p >= threshold), key=lambda t: (-t[1], t[0])
    )
    return ranked[:top_k]


def _actor_nation_map() -> dict[str, str]:
    """アクター id → 国家帰属 (ISO-2 小文字)。辞書が読めなければ空 (fail-open)。"""
    try:
        from src.cti.actor_normalizer import load_actor_aliases

        return {a.id: (a.nation or "").lower() for a in load_actor_aliases().actors if a.nation}
    except Exception as exc:  # noqa: BLE001 — 辞書不在でも採点は続ける
        _log.warning("detect_ml_actor_dict_unavailable", error=type(exc).__name__)
        return {}


def build_detect_articles(
    repo: Any, article_ids: Sequence[str], kinds: dict[str, str]
) -> dict[str, DetectArticle]:
    """DB 行 + entity 件数 + 種別 → DetectArticle (学習ハーネスと本番で同じ組み立て)。"""
    out: dict[str, DetectArticle] = {}
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    nation_of = _actor_nation_map()
    ids = list(dict.fromkeys(article_ids))
    with repo._connect() as conn:  # noqa: SLF001 — 読み取り専用の接続 seam 共有
        for i in range(0, len(ids), _CHUNK):
            chunk = ids[i : i + _CHUNK]
            ph = ",".join("?" * len(chunk))
            for r in conn.execute(
                "SELECT article_id, entity_type, value FROM article_entities "  # noqa: S608
                f"WHERE article_id IN ({ph})",
                tuple(chunk),
            ).fetchall():
                aid, etype = str(r["article_id"]), str(r["entity_type"])
                counts[aid][etype] += 1
                if etype == "involved_country":
                    counts[aid][f"country:{str(r['value'] or '').upper()}"] += 1
                elif etype == "actor":
                    nation = nation_of.get(str(r["value"] or ""))
                    if nation:
                        counts[aid][f"actor_nation:{nation}"] += 1
                    counts[aid][KNOWN_ACTOR_KEY] += 1
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
