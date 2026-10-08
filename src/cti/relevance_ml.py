"""日本関連性 (y_jp) の ML 分類器 — triage shadow 専用 (M4、2026-10-08)。

``data/mlx/relevance_ml_train_v2.py`` のオフライン実験 (dataset_v2, test split recall
98.3%/fire-rate 20.2%) で y_jp に最良だった構成 (embedding + feed_prior + gazetteer、
tfidf/hint は外す) をそのまま本番へ移す。モデル本体は ``config/models/relevance_jp_model.json``
(コード所有の学習済み資産、``config/models/detect_model.json`` と同じ扱い)。

**影子記録専用**: ``jp_probability`` / ``cascade_japan_involved`` の結果は
``triage_shadow`` に記録するだけで、本番の取り込み判定・配信は一切変えない。

学習時の embedding モデルと本番の埋込ティア割当が食い違うと特徴量の意味が変わるため、
``jp_probability`` は構築時に一致を確認し、不一致なら ``None`` を返して警告する
(sklearn 非依存、``detect_ml.py`` と同じ方針: 本番は dot product + sigmoid のみ)。
"""

from __future__ import annotations

import json
import math
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from src.logging_config import get_logger
from src.tools.embedding_client import EmbeddingClient
from src.tools.llm_client import LLMClient
from src.tools.model_tiers import Step, build_llm_for, resolve_embedding_model

if TYPE_CHECKING:
    from src.config_loader import AppConfig

_log = get_logger(__name__)

DEFAULT_MODEL_PATH = Path("config/models/relevance_jp_model.json")
DEFAULT_GAZETTEER_PATH = Path("data/mlx/jp_org_gazetteer_v1.json")
#: 学習時の入力 (title + body[:400])。relevance_ml_fetch_v2.py と同一でないと特徴量が合わない
PREVIEW_CHARS = 400
#: feed_prior_table の未知 feed フォールバックキー
_GLOBAL_PRIOR_KEY = "__global__"


@dataclass(frozen=True)
class RelevanceJpModel:
    """y_jp ロジスティック回帰 (embedding(1024, 標準化) + feed_prior(1) + gazetteer(1))。"""

    embedding_model: str
    coef: tuple[float, ...]
    intercept: float
    scaler_mean: tuple[float, ...]
    scaler_scale: tuple[float, ...]
    feed_prior_table: dict[str, float]
    gazetteer_names: tuple[str, ...]
    threshold_recall98: float
    threshold_recall99: float
    threshold_precision95: float

    @property
    def emb_dim(self) -> int:
        return len(self.scaler_mean)

    def probability(self, *, emb: Sequence[float], feed: str, gazetteer_hit: bool) -> float:
        if len(emb) != self.emb_dim:
            raise ValueError(f"embedding 次元が合いません: {len(emb)} != {self.emb_dim}")
        z = self.intercept
        for x, m, s, w in zip(
            emb, self.scaler_mean, self.scaler_scale, self.coef[: self.emb_dim], strict=True
        ):
            z += w * ((x - m) / s if s else 0.0)
        prior = self.feed_prior_table.get(feed, self.feed_prior_table.get(_GLOBAL_PRIOR_KEY, 0.0))
        z += self.coef[self.emb_dim] * prior
        z += self.coef[self.emb_dim + 1] * (1.0 if gazetteer_hit else 0.0)
        return 1.0 / (1.0 + math.exp(-z))


def _normalize(name: str) -> str:
    return unicodedata.normalize("NFKC", name or "").casefold().strip()


@lru_cache(maxsize=1)
def load_relevance_jp_model(path: Path | None = None) -> RelevanceJpModel | None:
    """JSON を読む。無い/形が壊れている → None (ML を使わない、fail-open)。"""
    target = path or DEFAULT_MODEL_PATH
    if not target.exists():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        gaz_path = Path(raw.get("gazetteer_path", DEFAULT_GAZETTEER_PATH))
        gaz_raw = json.loads(gaz_path.read_text(encoding="utf-8"))
        names = tuple(_normalize(e["name_norm"]) for e in gaz_raw["entries"] if e.get("name_norm"))
        return RelevanceJpModel(
            embedding_model=str(raw["embedding_model"]),
            coef=tuple(float(v) for v in raw["coef"]),
            intercept=float(raw["intercept"]),
            scaler_mean=tuple(float(v) for v in raw["scaler_mean"]),
            scaler_scale=tuple(float(v) for v in raw["scaler_scale"]),
            feed_prior_table={str(k): float(v) for k, v in raw["feed_prior_table"].items()},
            gazetteer_names=names,
            threshold_recall98=float(raw["threshold_recall98"]),
            threshold_recall99=float(raw["threshold_recall99"]),
            threshold_precision95=float(raw["threshold_precision95"]),
        )
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
        _log.warning("relevance_jp_model_invalid", path=str(target), error=type(exc).__name__)
        return None


def invalidate_relevance_jp_model_cache() -> None:
    """テスト / モデル更新後に明示的に cache を破棄したい呼び出し元向け。"""
    load_relevance_jp_model.cache_clear()


def _gazetteer_hit(text_norm: str, names: tuple[str, ...]) -> bool:
    return any(n in text_norm for n in names if len(n) >= 2)


async def jp_probability(
    *,
    feed: str,
    title: str,
    preview: str,
    embedder: EmbeddingClient,
    model: RelevanceJpModel | None = None,
) -> float | None:
    """y_jp (日本関連性) の確率。embedding 失敗 / モデル不在 / モデル不一致で ``None``。

    ``embedder`` は呼び出し側 (triage_shadow) が構築した本番埋込ティアのクライアント。
    学習時の embedding モデル (``model.embedding_model``) と不一致なら特徴量の意味が
    崩れるため使わない (構築ではなく呼び出し側 object の ``model`` 属性で確認する —
    ``EmbeddingClient`` Protocol には model 名が無いため、呼び出し側が渡した
    ``embedder.model`` を見る。属性が無いクライアントは一致確認できないため拒否)。
    """
    m = model or load_relevance_jp_model()
    if m is None:
        return None
    configured = getattr(embedder, "model", None)
    if configured != m.embedding_model:
        _log.warning(
            "relevance_jp_embedding_model_mismatch",
            configured=configured,
            expected=m.embedding_model,
        )
        return None
    text = f"{title} {preview[:PREVIEW_CHARS]}"
    try:
        resp = await embedder.embed(text, kind="document")
    except Exception as e:  # noqa: BLE001 — 1 件の embedding 失敗で呼び出し元を止めない
        _log.warning("relevance_jp_embedding_failed", error=str(e))
        return None
    gaz_hit = _gazetteer_hit(_normalize(f"{title} {preview[:PREVIEW_CHARS]}"), m.gazetteer_names)
    return m.probability(emb=resp.vector, feed=feed, gazetteer_hit=gaz_hit)


def ml_fired(probability: float, model: RelevanceJpModel) -> bool:
    """影子記録用の素の ML 発火判定 (99%-recall 閾値、カスケード前)。"""
    return probability >= model.threshold_recall99


def in_uncertain_band(probability: float, model: RelevanceJpModel) -> bool:
    """確信度が低い帯 (99%-recall 閾値 〜 95%-precision 閾値) か。帯内だけ LLM に聞く。"""
    return model.threshold_recall99 <= probability < model.threshold_precision95


class JapanInvolvementJudgement(BaseModel):
    """カスケード (案 D) の LLM 構造化出力。"""

    japan_involved: bool = Field(
        description="日本の組織・日本人・日本政府・日本が標的/被害当事者として関与するか"
    )
    reason: str = Field(description="判定理由 (1文)", max_length=200)


_CASCADE_PROMPT_TEMPLATE = """以下の記事 (タイトル+概要) が、日本の組織・日本のユーザ/市民・
日本政府・日本を標的/被害当事者として扱っているかを判定してください。

地名としての「日本」の言及のみ (当事者性が無い) は false としてください。

タイトル: {title}

概要:
{preview}
"""


async def cascade_japan_involved(
    *, title: str, preview: str, llm: LLMClient
) -> JapanInvolvementJudgement | None:
    """確信度が低い帯の記事だけに聞く小さな構造化判定 (fast ティア)。失敗時は None。"""
    prompt = _CASCADE_PROMPT_TEMPLATE.format(title=title, preview=preview[:PREVIEW_CHARS])
    try:
        return await llm.generate_structured(
            prompt,
            schema=JapanInvolvementJudgement,
            think=False,
            max_tokens=200,
        )
    except Exception as e:  # noqa: BLE001 — 1 件の失敗で影子記録全体を止めない
        _log.warning("relevance_jp_cascade_failed", error=str(e))
        return None


def build_relevance_jp_cascade_llm(config: AppConfig) -> LLMClient:
    """カスケード判定用 LLMClient (Step.RELEVANCE_JP_CASCADE、fast ティア)。"""
    return build_llm_for(Step.RELEVANCE_JP_CASCADE, config)


__all__ = [
    "DEFAULT_MODEL_PATH",
    "RelevanceJpModel",
    "JapanInvolvementJudgement",
    "load_relevance_jp_model",
    "invalidate_relevance_jp_model_cache",
    "jp_probability",
    "ml_fired",
    "in_uncertain_band",
    "cascade_japan_involved",
    "build_relevance_jp_cascade_llm",
    "resolve_embedding_model",
]
