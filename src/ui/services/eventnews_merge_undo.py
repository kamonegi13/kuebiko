"""統合を戻す (2026-10-22)。**戻す操作はここ 1 か所**。

統合 (``eventnews_merge_job.apply_merge_group``) は吸収側の事象の行を残し、
``merged_into`` を立てて、統合先に吸収側の記事を ``retro_merge`` で足す。
戻すとは、その逆を **統合先と、さらにその先へ吸収されている祖先すべて** に対して行うこと:

1. 吸収側の ``merged_into`` を外す (行も版も残っているので URL と本文が戻る)
2. 祖先から、吸収側の記事の ``retro_merge`` 行を消す (元からのメンバーの行は守る)
3. 祖先の状態 (重要度・時刻・媒体の内訳) をメンバーから作り直し、本文を作り直させる
4. 判断記録 (``event_merge_decisions``) を「戻した」にする

⚠ 3 の本文の再生成は呼び手 (毎時の段 / スクリプト) の責務。``current_version=0`` にしておけば
毎時の ``pending_items`` が拾う。状態の作り直しは遡及分割 (retro_split_events.py) と共有する。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.eventnews.models import MemberArticle
from src.eventnews.runner import _max_importance
from src.eventnews.state import compute_source_breakdown
from src.logging_config import get_logger
from src.storage.repo_merge_decisions import MAX_ROOT_HOPS
from src.storage.run_history import RunHistoryRepository
from src.ui.services.eventnews_merge_job import JOIN_SIGNAL

_log = get_logger(__name__)

STATUS_RESTORED = "restored"
STATUS_DRY_RUN = "dry_run"
STATUS_NOT_MERGED = "not_merged"
STATUS_MISSING = "missing"

#: 状態の作り直しに要るメンバー記事のローダ (``eventnews_hourly_job._load_members`` と同じ形)
MemberLoader = Callable[
    [Any, list[str], Mapping[tuple[str, str], int]], Mapping[str, MemberArticle]
]


@dataclass(frozen=True)
class UndoResult:
    """``undo_merge`` の結果。``status`` が ``restored`` のときだけ書き込んだ。"""

    absorbed_id: str
    status: str
    ancestors: tuple[str, ...] = ()
    articles: int = 0
    removed_rows: int = 0


def refresh_item_state(
    repo: RunHistoryRepository,
    item_id: str,
    members: Sequence[MemberArticle],
    *,
    now: datetime,
    clear_merged_into: bool,
) -> None:
    """メンバーから事象の状態を作り直し、本文を作り直させる (current_version=0)。

    ``clear_merged_into`` は遡及分割のように「この事象を生きた状態に戻す」ときだけ True。
    祖先の作り直しでは、祖先自身が別の事象に吸収されていることがあるので触らない。
    """
    breakdown = compute_source_breakdown(list(members))
    importance = ""
    for m in members:
        importance = _max_importance(importance, m.importance)
    fields: dict[str, object] = {
        "current_version": 0,  # メンバーが変わったので本文を作り直す
        "importance": importance,
        "best_source_tier": breakdown.best_tier,
        "independent_sources": breakdown.independent,
        "state_media_count": breakdown.state_media,
        "unclassified_sources": breakdown.unclassified,
        "first_reported_at": min(m.anchor_ts for m in members),
        "last_reported_at": max(m.anchor_ts for m in members),
        "updated_at": now,
    }
    if clear_merged_into:
        fields["merged_into"] = None
    repo.update_event_item(item_id, fields)


def _ancestors(repo: RunHistoryRepository, absorbed_id: str) -> tuple[str, ...]:
    """``merged_into`` を根までたどった祖先 (近い順)。循環・深すぎる連鎖は途中で打ち切る。"""
    out: list[str] = []
    cur = repo.get_event_item(absorbed_id)
    seen = {absorbed_id}
    while cur is not None and cur.merged_into and len(out) < MAX_ROOT_HOPS:
        nxt = cur.merged_into
        if nxt in seen:
            _log.warning("eventnews_merge_undo_cycle", absorbed_id=absorbed_id, at=nxt)
            break
        seen.add(nxt)
        out.append(nxt)
        cur = repo.get_event_item(nxt)
    return tuple(out)


def undo_merge(
    repo: RunHistoryRepository,
    absorbed_id: str,
    *,
    reason: str,
    now: datetime,
    apply: bool = True,
    load_members: MemberLoader | None = None,
) -> UndoResult:
    """吸収された事象 ``absorbed_id`` の統合を戻す。``apply=False`` は dry-run (何も書かない)。"""
    record = repo.get_event_item(absorbed_id)
    if record is None:
        return UndoResult(absorbed_id, STATUS_MISSING)
    if not record.merged_into:
        return UndoResult(absorbed_id, STATUS_NOT_MERGED)
    articles = [m.article_id for m in repo.list_event_members(absorbed_id)]
    ancestors = _ancestors(repo, absorbed_id)
    gone = set(articles)
    removable = sum(
        1
        for anc in ancestors
        for m in repo.list_event_members(anc)
        if m.join_signal == JOIN_SIGNAL and m.article_id in gone
    )
    if not apply:
        return UndoResult(
            absorbed_id, STATUS_DRY_RUN, ancestors, len(articles), removed_rows=removable
        )

    removed = 0
    for anc in ancestors:
        removed += repo.delete_event_members(anc, articles, only_join_signal=JOIN_SIGNAL)
    repo.update_event_item(absorbed_id, {"merged_into": None, "updated_at": now})
    loader = load_members or _default_loader
    for anc in ancestors:
        ids = [m.article_id for m in repo.list_event_members(anc)]
        members = list(loader(repo, ids, {}).values())
        if members:
            refresh_item_state(repo, anc, members, now=now, clear_merged_into=False)
    repo.mark_merge_decisions_undone(absorbed_id, reason=reason, undone_at=now)
    _log.info(
        "eventnews_merge_undone",
        absorbed_id=absorbed_id,
        ancestors=len(ancestors),
        removed_rows=removed,
    )
    return UndoResult(absorbed_id, STATUS_RESTORED, ancestors, len(articles), removed)


def _default_loader(
    repo: Any, ids: list[str], counts: Mapping[tuple[str, str], int]
) -> Mapping[str, MemberArticle]:
    from src.ui.services.eventnews_hourly_job import _load_members

    return _load_members(repo, ids, counts)
