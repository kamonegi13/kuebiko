"""導いた線の日次の観測の写し (``event_edge_records``、2026-11-02 GRAPH-G0)。

導出が SSoT の作り直せる写し。1 日 1 回、同じ ``derived_at`` の再実行は行を置き換える
(冪等)。本文は持たない。retention 180 日 (daily-maintenance)。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from src.storage.repo_base import RunHistoryRepositoryBase
from src.storage.repo_merge_decisions import _follow, _merged_into_map
from src.storage.row_mappers import _to_iso

DEFAULT_RETENTION_DAYS = 180
_CHUNK = 400


class _RecordLike(Protocol):
    """``graph.edge_records.EdgeRecord`` の形 (storage 層から graph を import しない)。"""

    @property
    def derived_at(self) -> str: ...

    @property
    def edge_key(self) -> str: ...

    @property
    def item_a(self) -> str: ...

    @property
    def item_b(self) -> str: ...

    @property
    def root_a(self) -> str: ...

    @property
    def root_b(self) -> str: ...

    @property
    def edge_type(self) -> str: ...

    @property
    def rel_type(self) -> str: ...

    @property
    def source(self) -> str: ...

    @property
    def basis_json(self) -> str: ...

    @property
    def score(self) -> float | None: ...

    @property
    def rule_version(self) -> str: ...

    @property
    def model_sha(self) -> str: ...

    @property
    def tier(self) -> str: ...


def _cutoff_date(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).date().isoformat()


class EventEdgeRecordsMixin(RunHistoryRepositoryBase):
    """``event_edge_records`` の書き込み・集計・保持。"""

    def replace_event_edge_records(self: Any, derived_at: str, rows: Sequence[_RecordLike]) -> int:
        """``derived_at`` の行を全部消して ``rows`` を入れ直す (同じ日の再実行は置き換え)。"""
        now = _to_iso(datetime.now(UTC))
        with self._connect() as conn:
            conn.execute("DELETE FROM event_edge_records WHERE derived_at = ?", (derived_at,))
            for r in rows:
                conn.execute(
                    "INSERT INTO event_edge_records (derived_at, edge_key, item_a, item_b,"
                    " root_a, root_b, edge_type, rel_type, source, basis_json, score,"
                    " rule_version, model_sha, tier, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        derived_at,
                        r.edge_key,
                        r.item_a,
                        r.item_b,
                        r.root_a,
                        r.root_b,
                        r.edge_type,
                        r.rel_type,
                        r.source,
                        r.basis_json,
                        r.score,
                        r.rule_version,
                        r.model_sha,
                        r.tier,
                        now,
                    ),
                )
        return len(rows)

    def edge_record_stats(self: Any, derived_at: str) -> dict[str, int]:
        """``derived_at`` の写しの件数を線の型ごとに。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT edge_type, COUNT(*) AS n FROM event_edge_records"
                " WHERE derived_at = ? GROUP BY edge_type",
                (derived_at,),
            ).fetchall()
        return {str(r["edge_type"]): int(r["n"]) for r in rows}

    def purge_event_edge_records(self: Any, days: int = DEFAULT_RETENTION_DAYS) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM event_edge_records WHERE derived_at < ?", (_cutoff_date(days),)
            )
            return int(cur.rowcount or 0)

    def event_roots(self: Any, item_ids: Sequence[str]) -> dict[str, str]:
        """事象 id → ``merged_into`` をたどった根。"""
        ids = list(dict.fromkeys(item_ids))
        if not ids:
            return {}
        with self._connect() as conn:
            parent = _merged_into_map(conn, set(ids))
        return {i: _follow(parent, i) for i in ids}


__all__ = ["DEFAULT_RETENTION_DAYS", "EventEdgeRecordsMixin"]
