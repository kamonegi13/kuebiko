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
    AlertDedupMatch,
    build_current_entities,
    find_prior_alert_duplicate,
    has_new_named_entities,
    is_alert_dedup_event_enabled,
)
from src.cti.law_enforcement_gate import (
    has_law_enforcement_keyword,
    is_law_enforcement_gate_enabled,
)
from src.logging_config import get_logger

if TYPE_CHECKING:
    from datetime import datetime

    from src.config_loader import AppConfig
    from src.storage.run_history import RunHistoryRepository
    from src.tools.discord_publisher import BriefingMessage

_log = get_logger(__name__)

#: 降格先 channel (利用者決定: watch/日次で読む)。
DEMOTED_CHANNEL = "watch"

#: 事象重複ゲート: pair_model の P(同じ出来事) がこの値以上なら entity-diff を経ずに
#: 抑止する (高確信 bypass、2026-10-11 コーディネータ指示)。backtest で校正
#: (data/mlx/analyze_r1b_dedup_options.py、data/eval/r1b_report.md §8):
#: 52 件中 11 件抑止 (21.2%)・新事実 21 件中 1 件誤抑止 (4.8%、目標 <=1/21 を満たす最大値)。
#: 別名解決で entity-diff を緩める案 (victim_org 正規化・actor resolve_actor_id) は
#: 新事実誤抑止が 3/21=14.3% に悪化 (目標超過) のため採用しなかった。
HIGH_CONFIDENCE_BYPASS_THRESHOLD = 0.94


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
    # 語の条件だけで判定する。logprob の判定 (退役済みの alert_law_enforcement_judge、P0-05) は
    # 校正 (185 件、2026-10-11) で s23i・gemma4:26b とも P(はい) がほぼ全件 0.5 超 —
    # 本番に使うと alert のほぼ全件が降格する。語の条件は精度 10/11・回収 10/10
    if is_law_enforcement_gate_enabled() and has_law_enforcement_keyword(
        msg.title or "", msg.summary or ""
    ):
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
        if dedup_match is not None and _should_suppress_duplicate(dedup_match, current_entities):
            return AlertDemotionResult(f"alert_dedup_event:{dedup_match.reason}")
    return None


def _should_suppress_duplicate(
    dedup_match: AlertDedupMatch, current_entities: frozenset[tuple[str, str]]
) -> bool:
    """同事象の重複として watch へ降格すべきか。

    完全一致 (dedup_key/advisory_id) は常に抑止。pair_model 一致は、確率が高確信
    bypass 閾値以上ならそのまま抑止 (entity-diff を経ない)。それ以外は entity-diff
    (``has_new_named_entities``) で「新事実の続報」を見送る。
    """
    if dedup_match.reason in ("dedup_key", "advisory_id"):
        return True
    if dedup_match.probability >= HIGH_CONFIDENCE_BYPASS_THRESHOLD:
        return True
    return not has_new_named_entities(current_entities, dedup_match.prior_entities)


__all__ = [
    "DEMOTED_CHANNEL",
    "AlertDemotionResult",
    "maybe_demote_alert",
]
