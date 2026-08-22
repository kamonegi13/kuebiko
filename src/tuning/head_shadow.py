"""蒸留ヘッド v0 のシャドー推論 (§14.3、2026-08-22)。

triage 直後に全候補 (通過 + 棄却) をヘッドで予測し、triage LLM の判定との対を
head_shadow テーブルへ記録する。**本番の配信判断には一切影響しない** — 成果物不在・
embedding 版不一致・例外はすべて fail-open (skip + log)。

不変条件 12 (足切りの自動化は死角を作らない) の観測点: disagree_cutoff が
「ヘッドなら残す記事を LLM が切った / 逆」の初の永続記録になる。

成果物は data/models/head/current (scripts/train_head.py が生成)。metadata.json の
mtime で再読込を判定するため、再学習後の反映にプロセス再起動は不要。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import structlog

from src.tools.url_normalizer import url_hash

if TYPE_CHECKING:
    from src.storage.run_history import RunHistoryRepository
    from src.tools.article_model import Article
    from src.tuning.head_model import HeadBundle, HeadMetadata

_log = structlog.get_logger(__name__)

ENV_FLAG = "HEAD_SHADOW"
DEFAULT_ARTIFACT_DIR = Path("data/models/head/current")

# (metadata.json の mtime, bundle, metadata)。mtime 変化で再読込。
_cached: tuple[float, HeadBundle, HeadMetadata] | None = None


def _is_enabled() -> bool:
    """HEAD_SHADOW flag (既定 on)。"0" で完全停止。"""
    return os.environ.get(ENV_FLAG, "1").strip() != "0"


def reset_cache() -> None:
    """テスト用: 成果物キャッシュを破棄する。"""
    global _cached
    _cached = None


def _load_bundle(artifact_dir: Path) -> tuple[HeadBundle, HeadMetadata] | None:
    """成果物を mtime キャッシュ付きで読み込む。不在・破損は None (fail-open)。"""
    global _cached
    meta_path = artifact_dir / "metadata.json"
    if not meta_path.is_file():
        return None
    try:
        mtime = meta_path.stat().st_mtime
        if _cached is not None and _cached[0] == mtime:
            return _cached[1], _cached[2]
        from src.tuning.head_model import load_artifact

        # 版バインド検証 (embedding モデル名) は load_artifact 側。ここでは
        # metadata の宣言をそのまま期待値に渡し、per-vector の一致は呼出側で確認する
        declared = json.loads(meta_path.read_text(encoding="utf-8"))
        bundle, meta = load_artifact(
            artifact_dir, expected_embedding_model=str(declared["embedding_model"])
        )
        _cached = (mtime, bundle, meta)
        _log.info(
            "head_artifact_loaded",
            artifact_version=meta.artifact_version,
            embedding_model=meta.embedding_model,
        )
        return bundle, meta
    except Exception as e:  # noqa: BLE001 — 成果物破損でシャドーを止めるだけ (本番不干渉)
        _log.warning("head_artifact_load_failed", error=str(e))
        return None


def record_head_shadow(
    *,
    decisions: list[tuple[Article, str, bool]],
    embeddings: dict[str, tuple[str, list[float]]],
    kept_ids: set[str],
    keep_importance: set[str],
    repo: RunHistoryRepository,
    run_id: str | None,
    artifact_dir: Path = DEFAULT_ARTIFACT_DIR,
) -> int:
    """triage 全判定に対するヘッド予測を記録する。戻り値は記録件数。

    decisions は _filter_by_triage の露出値 (article, importance, error)。
    embeddings は Phase 1.7 の計算済み vector (article_id → (model, vector))。
    """
    if not _is_enabled():
        return 0
    loaded = _load_bundle(artifact_dir)
    if loaded is None:
        return 0
    bundle, meta = loaded

    pending: list[tuple[Article, str, bool]] = []
    vectors: list[list[float]] = []
    for article, importance, error in decisions:
        emb = embeddings.get(article.id)
        if emb is None:
            continue
        emb_model, vec = emb
        # 版バインド (§14.2): 学習時と異なる embedding モデル・次元は予測しない
        if emb_model != meta.embedding_model or len(vec) != meta.dim:
            continue
        pending.append((article, importance, error))
        vectors.append(vec)
    if not pending:
        return 0

    from src.storage.repo_head_shadow import HeadShadowRecord
    from src.tuning.head_model import predict_batch

    matrix = np.asarray(vectors, dtype=np.float32)
    predictions = predict_batch(bundle, matrix)

    records: list[HeadShadowRecord] = []
    for (article, importance, error), pred in zip(pending, predictions, strict=True):
        # 足切り境界の不一致は判定水準で比較する (max_keep の枠あふれは予算の
        # 問題であり判断の不一致ではない)。実際の生存は triage_kept が持つ。
        head_keep = pred.importance in keep_importance
        llm_keep = importance in keep_importance
        records.append(
            HeadShadowRecord(
                url_hash=url_hash(article.url),
                article_id=article.id,
                run_id=run_id,
                head_importance=pred.importance,
                head_importance_probs=json.dumps(
                    pred.importance_probs, ensure_ascii=False, sort_keys=True
                ),
                head_category=pred.category,
                head_category_prob=pred.category_prob,
                triage_importance=importance,
                triage_error=error,
                triage_kept=article.id in kept_ids,
                disagree_cutoff=head_keep != llm_keep,
                artifact_version=meta.artifact_version,
                embedding_model=meta.embedding_model,
            )
        )
    return repo.record_head_shadow(records)
