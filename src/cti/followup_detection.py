"""Phase B: Discord 続報 marker — 同 incident の続報を検出して BriefingMessage に注釈付与。

dedup の 48h 強制 skip 機構とは独立した検出層:
    - 48h window 内の重複は既に skip される
    - 48h 超え 168h 以内の同 incident 投稿は **続報として許容**
      (memory `[dedup_window_48h_design_intent]`)
    - 本モジュールはその「続報として通った」投稿に対して、Discord 投稿時の視認性を上げる
      ``followup_info`` を metadata に注入する

同 incident の判定は 2 段階 (Phase B-cal):
    1. **dedup_key 完全一致** (高速・厳密)。LLM/CVE 由来の安定キーが一致する場合。
    2. **embedding 類似度** (頑健)。dedup_key が一致しなくても、過去 168h の投稿済み記事と
       コサイン類似度 >= threshold なら同 incident の続報と見なす。dedup_key は LLM が
       記事ごとに微妙に異なる slug を生成しがちで完全一致が稀なため、semantic を主軸にする。

入力: BriefingMessage、RunHistoryRepository、(任意) 当該記事の embedding。
出力: BriefingMessage.metadata に ``followup_info: {prior_count, prior_at_iso, prior_title}``
      が付与された新 instance (Pydantic frozen なので model_copy で返す)。
"""

from __future__ import annotations

from collections.abc import Sequence

from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository
from src.tools.discord_publisher import BriefingMessage

_log = get_logger(__name__)

# 続報判定 window: 過去 N 時間以内の投稿を続報候補と見なす
DEFAULT_FOLLOWUP_LOOKBACK_HOURS = 168  # 7 日
# semantic 続報の類似度閾値。hard-dedup (0.92、同一記事) より低く、トピック類似
# (0.78 cluster) より高い = 「同 incident だが別記事 (続報)」を狙う保守的な値。
DEFAULT_FOLLOWUP_SIM_THRESHOLD = 0.88


def annotate_followup(
    message: BriefingMessage,
    *,
    repo: RunHistoryRepository,
    embedding: Sequence[float] | None = None,
    embedding_model: str = "",
    lookback_hours: int = DEFAULT_FOLLOWUP_LOOKBACK_HOURS,
    sim_threshold: float = DEFAULT_FOLLOWUP_SIM_THRESHOLD,
) -> BriefingMessage:
    """``message`` に続報情報を注入した新しい BriefingMessage を返す。

    続報が検出されない場合は元の message をそのまま返す。
    例外発生時も swallow して元 message を返す (Discord 投稿を壊さない)。
    """
    try:
        info = _detect_dedup_key(message, repo, lookback_hours) or _detect_semantic(
            embedding, embedding_model, repo, lookback_hours, sim_threshold
        )
    except Exception as e:  # noqa: BLE001
        _log.warning(
            "followup_detection_failed",
            error=f"{type(e).__name__}: {e}",
        )
        return message
    if info is None:
        return message

    new_metadata = dict(message.metadata)
    new_metadata["followup_info"] = info
    _log.info(
        "followup_marker_applied",
        via=info.get("via"),
        prior_at=info.get("prior_at_iso"),
        similarity=info.get("similarity"),
    )
    return message.model_copy(update={"metadata": new_metadata})


def _detect_dedup_key(
    message: BriefingMessage,
    repo: RunHistoryRepository,
    lookback_hours: int,
) -> dict[str, object] | None:
    """dedup_key 完全一致での続報検出 (primary)。"""
    dedup_key = str(message.metadata.get("dedup_key") or "").strip()
    if not dedup_key:
        return None
    prior_count, prior_record = repo.count_prior_posts_by_dedup_key(
        dedup_key, lookback_hours=lookback_hours
    )
    if prior_count == 0 or prior_record is None:
        return None
    return {
        "via": "dedup_key",
        "prior_count": prior_count,
        "prior_at_iso": _iso(prior_record.created_at),
        "prior_title": (prior_record.title or "")[:80],
    }


def _detect_semantic(
    embedding: Sequence[float] | None,
    embedding_model: str,
    repo: RunHistoryRepository,
    lookback_hours: int,
    sim_threshold: float,
) -> dict[str, object] | None:
    """embedding 類似度での続報検出 (dedup_key が一致しなかった時の fallback)。"""
    if embedding is None or not embedding_model:
        return None
    match = repo.find_similar_embedding(
        embedding,
        model=embedding_model,
        threshold=sim_threshold,
        window_hours=lookback_hours,
    )
    if match is None:
        return None
    prior_hash, sim = match
    prior = repo.get_seen_url(prior_hash)
    prior_title, prior_at = ("", None) if prior is None else prior
    return {
        "via": "semantic",
        "prior_count": 1,
        "prior_at_iso": _iso(prior_at) if prior_at is not None else "",
        "prior_title": prior_title[:80],
        "similarity": round(sim, 3),
    }


def _iso(dt: object) -> str:
    return dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
