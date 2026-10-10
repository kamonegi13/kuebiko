"""M3b 段1: routing v2 (重要度の再設計ベース) の shadow 記録 (``routing_v2_shadow`` テーブル)。

本番の投稿先 (v12 現行ルール) は変えない。「現行ルール (old) と M3B_RULES (new) が
食い違う件数」を追跡する安全網。``triage_shadow`` (2026-10-08) と同じ append-only +
retention の形。詳細設計: docs/importance_relevance_redesign.md。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from src.storage.repo_base import RunHistoryRepositoryBase
from src.storage.row_mappers import _to_iso

DEFAULT_LIST_LIMIT = 100
DEFAULT_RETENTION_DAYS = 60


@dataclass(frozen=True)
class RoutingV2ShadowRow:
    """影子記録 1 件 (1 記事 = route() 時点の 1 回のみ記録)。"""

    article_id: str
    old_channel: str
    old_rule_id: str
    new_channel: str
    new_rule_id: str
    severity: str
    severity_basis: str
    jp: str
    relevant: bool
    importance: str
    created_at: str = ""

    @property
    def changed(self) -> bool:
        return self.old_channel != self.new_channel


@dataclass(frozen=True)
class RoutingV2ShadowCrosstabCell:
    """old_channel × new_channel の 1 セル。"""

    old_channel: str
    new_channel: str
    count: int


def _cutoff(days: int) -> str:
    return _to_iso(datetime.now(UTC) - timedelta(days=days))


class RoutingV2ShadowMixin(RunHistoryRepositoryBase):
    """``routing_v2_shadow`` テーブルの読み書き。"""

    def record_routing_v2_shadow(self: Any, row: RoutingV2ShadowRow) -> None:
        """1 記事の shadow 判定を追記する (append-only)。"""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO routing_v2_shadow (article_id, created_at, old_channel,"
                " old_rule_id, new_channel, new_rule_id, severity, severity_basis, jp,"
                " relevant, importance) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row.article_id,
                    _to_iso(datetime.now(UTC)),
                    row.old_channel,
                    row.old_rule_id,
                    row.new_channel,
                    row.new_rule_id,
                    row.severity,
                    row.severity_basis,
                    row.jp,
                    1 if row.relevant else 0,
                    row.importance,
                ),
            )

    def summarize_routing_v2_shadow(self: Any, *, days: int) -> list[RoutingV2ShadowCrosstabCell]:
        """直近 ``days`` 日の old_channel × new_channel の件数 (go/no-go 用)。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT old_channel, new_channel, COUNT(*) AS n FROM routing_v2_shadow"
                " WHERE created_at >= ? GROUP BY old_channel, new_channel",
                (_cutoff(days),),
            ).fetchall()
        return [
            RoutingV2ShadowCrosstabCell(
                old_channel=str(r["old_channel"]),
                new_channel=str(r["new_channel"]),
                count=int(r["n"]),
            )
            for r in rows
        ]

    def list_routing_v2_shadow_changed(
        self: Any, *, days: int, limit: int = DEFAULT_LIST_LIMIT
    ) -> list[RoutingV2ShadowRow]:
        """old と new の投稿先が食い違った記事を新しい順に返す (画面/API 用)。"""
        sql = (
            "SELECT article_id, old_channel, old_rule_id, new_channel, new_rule_id,"
            " severity, severity_basis, jp, relevant, importance, created_at, id"
            " FROM routing_v2_shadow WHERE created_at >= ? AND old_channel <> new_channel"
            " ORDER BY created_at DESC, id DESC LIMIT ?"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, (_cutoff(days), int(limit))).fetchall()
        return [
            RoutingV2ShadowRow(
                article_id=str(r["article_id"]),
                old_channel=str(r["old_channel"]),
                old_rule_id=str(r["old_rule_id"]),
                new_channel=str(r["new_channel"]),
                new_rule_id=str(r["new_rule_id"]),
                severity=str(r["severity"]),
                severity_basis=str(r["severity_basis"]),
                jp=str(r["jp"]),
                relevant=bool(r["relevant"]),
                importance=str(r["importance"]),
                created_at=str(r["created_at"]),
            )
            for r in rows
        ]

    def purge_routing_v2_shadow(self: Any, days: int = DEFAULT_RETENTION_DAYS) -> int:
        """``days`` 日より古い影子記録を削除する。"""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM routing_v2_shadow WHERE created_at < ?", (_cutoff(days),)
            )
            return int(cur.rowcount or 0)


__all__ = [
    "DEFAULT_LIST_LIMIT",
    "DEFAULT_RETENTION_DAYS",
    "RoutingV2ShadowCrosstabCell",
    "RoutingV2ShadowMixin",
    "RoutingV2ShadowRow",
]
