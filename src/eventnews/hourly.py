"""毎時運用の入口 — 窓内の既存アイテムを復元し、新着記事だけを逐次適用する。

**リプレイと本番で同じ ``runner.process_candidates`` を使う** (評価と本番の挙動差を
構造的に無くすため)。この module が受け持つのは「状態の復元」と「候補の限定」だけ。

v1 は shadow: この関数はスケジューラに登録しない (scripts から手動で呼ぶ)。
本番配線は cutover 判断の後 (docs/event_news_design.md §12)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np

from src.eventnews.models import DORMANT_AFTER_DAYS, WINDOW_HOURS, ItemState, MemberArticle
from src.eventnews.runner import ProcessStats, process_candidates
from src.logging_config import get_logger
from src.storage.repo_eventnews import EventNewsMixin

_log = get_logger(__name__)

# 復元対象の窓。参加判定は last_reported_at から WINDOW_HOURS だが、dormant への
# 再参加 (厳条件) も許すため、復元は dormant 期限まで遡る。
_HYDRATE_HOURS = max(WINDOW_HOURS, DORMANT_AFTER_DAYS * 24)


@dataclass(frozen=True)
class HourlyResult:
    stats: ProcessStats
    hydrated_items: int
    candidates: int


def hydrate_open_items(
    repo: EventNewsMixin,
    load_members: object,  # Callable[[Sequence[str]], dict[str, MemberArticle]]
    *,
    now: datetime | None = None,
) -> list[tuple[ItemState, list[MemberArticle]]]:
    """窓内の live アイテムを状態 + メンバーごと復元する。

    ``load_members`` は article_id 群 → MemberArticle の写像を返す呼び出し可能。
    記事本文の取得は呼び手 (scripts / job) の責務で、この module は DB 形状だけを知る。
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(hours=_HYDRATE_HOURS)
    records = repo.list_event_items(origin="live", limit=5000)
    open_items = [r for r in records if r.state.last_reported_at >= cutoff and not r.merged_into]
    article_ids = [aid for r in open_items for aid in r.state.member_ids]
    members_by_id = load_members(article_ids)  # type: ignore[operator]
    out: list[tuple[ItemState, list[MemberArticle]]] = []
    for r in open_items:
        members = [members_by_id[a] for a in r.state.member_ids if a in members_by_id]
        if members:
            out.append((r.state, members))
    _log.info(
        "eventnews_hydrated",
        items=len(out),
        skipped_no_members=len(open_items) - len(out),
        cutoff=cutoff.isoformat(),
    )
    return out


async def run_hourly(
    repo: EventNewsMixin,
    candidates: Sequence[MemberArticle],
    vectors: dict[str, np.ndarray],
    existing: Sequence[tuple[ItemState, list[MemberArticle]]],
    llm_factory: object,  # Callable[[], LLMClient] | None
    *,
    pair_decision: Mapping[frozenset[str], bool] | None = None,
    pair_proba: Mapping[frozenset[str], float] | None = None,
) -> HourlyResult:
    """新着記事のみを既存状態の続きとして処理する。

    ``candidates`` は **前回実行以降に取り込まれた記事**に限ること (全件を渡すと
    既存メンバーが再評価され、``add_event_member`` の冪等性に依存した無駄な走査になる)。
    """
    already = {a for _, members in existing for m in members for a in (m.article_id,)}
    fresh = [c for c in candidates if c.article_id not in already]
    stats = await process_candidates(
        repo,
        list(fresh),
        vectors,
        "live",
        llm_factory,  # type: ignore[arg-type]
        generate=True,
        existing=existing,
        pair_decision=pair_decision,
        pair_proba=pair_proba,
    )
    _log.info(
        "eventnews_hourly_done",
        candidates=len(fresh),
        skipped_already_member=len(candidates) - len(fresh),
        created=stats.created,
        updated=stats.updated,
        reinforced=stats.reinforced,
        generated=stats.generated,
    )
    return HourlyResult(stats=stats, hydrated_items=len(existing), candidates=len(fresh))


__all__ = ["HourlyResult", "hydrate_open_items", "run_hourly"]
