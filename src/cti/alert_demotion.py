"""alert channel demotion を 2 関門で統合する薄い層 (R1b、2026-10-11)。

利用者決定 (10-11):
    1. 当局 (警察・検察等) による摘発・無力化・起訴・押収・逮捕・制裁指定の発表は
       alert にしない (``src.cti.law_enforcement_gate``)。
    2. 同じ事象への重複 alert を止める。新事実の続報は残す
       (``src.cti.alert_dedup_gate``、設計上の限界は同モジュールの docstring 参照)。

``orchestrator.py`` の投稿ループから 1 回の非同期呼出で済ませるための統合層。
両関門とも既定 off (env flag)。どちらも off なら常に ``None`` (降格なし、挙動不変)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.cti.alert_dedup_gate import (
    build_current_entities,
    find_prior_alert_duplicate,
    has_new_named_entities,
    is_alert_dedup_event_enabled,
)
from src.cti.law_enforcement_gate import (
    has_law_enforcement_keyword,
    is_law_enforcement_gate_enabled,
    probe_law_enforcement_announcement,
)
from src.logging_config import get_logger

if TYPE_CHECKING:
    from datetime import datetime

    from src.config_loader import AppConfig
    from src.storage.run_history import RunHistoryRepository
    from src.tools.discord_publisher import BriefingMessage

_log = get_logger(__name__)

#: probe の P(はい) をこの値より大きければ law_enforcement 扱いにする (多数決の仮値)。
#: gold set での校正は未実施 — data/eval/r1b_report.md の設計を参照 (裁定は承認後)。
LAW_ENFORCEMENT_PROBE_THRESHOLD = 0.5

#: 降格先 channel (利用者決定: watch/日次で読む)。
DEMOTED_CHANNEL = "watch"


@dataclass(frozen=True)
class AlertDemotionResult:
    """降格理由。``routing_reason``/log に使う。"""

    reason: str  # "law_enforcement_announcement" | "alert_dedup_event:<match reason>"


async def maybe_demote_alert(
    msg: BriefingMessage,
    *,
    article_url: str,
    article_id: str,
    category: str,
    feed_title: str,
    published_at: datetime,
    embedding: tuple[str, list[float]] | None,
    config: AppConfig,
    dedup_repo: RunHistoryRepository | None,
) -> AlertDemotionResult | None:
    """alert 候補を watch へ降格すべきか判定する (2 関門とも既定 off)。

    ``embedding`` は ``(model, vector)`` — 本文 embedding (semantic dedup で既に計算済みの
    もの、``orchestrator.py`` の ``semantic_embeddings`` を渡す)。未計算 (None) なら
    事象重複ゲートの embedding/pair_model 段は素通りする (dedup_key/advisory id の
    完全一致だけ有効)。
    """
    if is_law_enforcement_gate_enabled():
        is_law_enforcement = await _is_law_enforcement(
            msg.title or "", msg.summary or "", config=config
        )
        if is_law_enforcement:
            return AlertDemotionResult("law_enforcement_announcement")

    if is_alert_dedup_event_enabled() and dedup_repo is not None:
        current_entities = build_current_entities(
            iocs=msg.iocs,
            mitre_techniques=msg.mitre_techniques,
            malware_families=list(msg.metadata.get("malware_families") or []),
            tools=list(msg.metadata.get("tools") or []),
            detected_actor_ids=list(msg.metadata.get("detected_actor_ids") or []),
            victim_orgs=list(msg.metadata.get("victim_orgs") or []),
        )
        dedup_match = find_prior_alert_duplicate(
            repo=dedup_repo,
            dedup_key=str(msg.metadata.get("dedup_key") or ""),
            title=msg.title or "",
            summary=msg.summary or "",
            url=article_url,
            category=category,
            feed_title=feed_title,
            published_at=published_at,
            vector=embedding[1] if embedding else None,
            embedding_model=embedding[0] if embedding else "",
            entities=current_entities,
            candidate_article_id=article_id,
        )
        if dedup_match is not None and not has_new_named_entities(
            current_entities, dedup_match.prior_entities
        ):
            return AlertDemotionResult(f"alert_dedup_event:{dedup_match.reason}")
    return None


async def _is_law_enforcement(title: str, summary: str, *, config: AppConfig) -> bool:
    """probe 優先、``None`` (外部 LLM 割当・denylist 抵触・通信失敗) なら決定的な語の条件へ退避。"""
    p_yes = await probe_law_enforcement_announcement(title=title, summary=summary, config=config)
    if p_yes is not None:
        return p_yes > LAW_ENFORCEMENT_PROBE_THRESHOLD
    return has_law_enforcement_keyword(title, summary)


__all__ = [
    "DEMOTED_CHANNEL",
    "LAW_ENFORCEMENT_PROBE_THRESHOLD",
    "AlertDemotionResult",
    "maybe_demote_alert",
]
