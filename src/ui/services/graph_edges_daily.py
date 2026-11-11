"""導いた線の日次の写し (GRAPH-G0、2026-11-02)。

``relations_by_event`` の導出 + 焦点の lineage (統合を止めた関係の候補・統合の判断記録)
を ``event_edge_records`` に写す。**記録だけ** (配信・画面・重要度は変えない)。LLM 不使用。

- 旗 ``EVENT_EDGES_RECORD`` (コードの既定 0、1 で書く)。戻し = 0 (記録が止まるだけ)
- 同じ UTC 日の再実行は行を置き換える (冪等)
- 記録するのは ``index_relations`` の上限 (1 事象あたり same_actor 5・incident 10) の
  **後** の線 (上限で落ちた線は写らない)
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.eventnews.identity.focus import CLASS_LINEAGE, MATCH_RULE_VERSION
from src.eventnews.relation_model import DEFAULT_MODEL_PATH
from src.eventnews.relations import WINDOW_DAYS, DerivedRelation, relations_by_event
from src.graph.edge_records import (
    SOURCE_DECISION,
    SOURCE_VETO,
    EdgeRecord,
    records_from_lineage,
    records_from_relations,
)
from src.logging_config import get_logger
from src.storage.repo_merge_decisions import ACTION_HUB_SPLIT_BLOCKED, ACTION_RELATION_CANDIDATE
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)

FLAG = "EVENT_EDGES_RECORD"
#: 焦点の lineage を拾う期間 (導出の窓と同じ)
LINEAGE_WINDOW_DAYS = WINDOW_DAYS
_LINEAGE_LIMIT = 5000
_SHA_LEN = 12


def edges_record_enabled() -> bool:
    return os.environ.get(FLAG, "0").strip() == "1"


def relation_model_sha(path: Path = DEFAULT_MODEL_PATH) -> str:
    """分類器の JSON の sha256 先頭。無ければ空 (分類器なし = 同じアクターだけ)。"""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:_SHA_LEN]
    except OSError:
        return ""


def flatten_relations(index: dict[str, list[DerivedRelation]]) -> list[DerivedRelation]:
    return [r for rels in index.values() for r in rels]


def collect_lineage_pairs(repo: Any, *, now: datetime) -> list[tuple[str, str, str]]:
    """焦点の lineage の組 ``(a, b, source)``: 統合を止めた関係の候補 + 統合の判断記録。

    勧告のハブの関門 (EVENTNEWS_MERGE_HUB_SPLIT=1) で結ばなかった組も系統の候補として読む。
    """
    since = now - timedelta(days=LINEAGE_WINDOW_DAYS)
    pairs: list[tuple[str, str, str]] = []
    for v in repo.list_merge_vetoes(
        since=since,
        actions=[ACTION_RELATION_CANDIDATE, ACTION_HUB_SPLIT_BLOCKED],
        limit=_LINEAGE_LIMIT,
    ):
        pairs.append((v.item_a, v.item_b, SOURCE_VETO))
    for d in repo.list_merge_decisions(limit=_LINEAGE_LIMIT):
        decided = d.decided_at
        if d.focus_class != CLASS_LINEAGE or d.undone_at is not None:
            continue
        if decided is not None and decided.replace(tzinfo=decided.tzinfo or UTC) < since:
            continue
        pairs.append((d.target_id, d.absorbed_id, SOURCE_DECISION))
    return pairs


def build_records(
    repo: Any, *, derived_at: str, now: datetime, index: dict[str, list[DerivedRelation]]
) -> list[EdgeRecord]:
    rows = records_from_relations(
        flatten_relations(index), derived_at=derived_at, model_sha=relation_model_sha()
    )
    pairs = collect_lineage_pairs(repo, now=now)
    ids = sorted({i for a, b, _ in pairs for i in (a, b)})
    roots = repo.event_roots(ids)
    rows += records_from_lineage(
        pairs, derived_at=derived_at, roots=roots, rule_version=MATCH_RULE_VERSION
    )
    return rows


async def run_graph_edges_daily(repo: RunHistoryRepository | None = None) -> dict[str, Any]:
    """日次ジョブ本体。戻り値は件数だけ。"""
    if not edges_record_enabled():
        _log.info("graph_edges_daily_disabled")
        return {"skipped": "flag_off"}
    repo = repo or RunHistoryRepository()
    now = datetime.now(UTC)
    derived_at = now.date().isoformat()
    # 初回は全事象を読む同期処理 (数十秒) — イベントループを止めない
    index = await asyncio.to_thread(relations_by_event, repo)
    # 同期の DB 処理 (判断記録の読み・約 6,000 行の書き込み) もイベントループの外で
    rows = await asyncio.to_thread(build_records, repo, derived_at=derived_at, now=now, index=index)
    written = await asyncio.to_thread(repo.replace_event_edge_records, derived_at, rows)
    stats = repo.edge_record_stats(derived_at)
    _log.info("graph_edges_daily_done", derived_at=derived_at, written=written, by_type=stats)
    return {"derived_at": derived_at, "written": written, **stats}


__all__ = ["FLAG", "edges_record_enabled", "run_graph_edges_daily"]
