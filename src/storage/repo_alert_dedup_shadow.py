"""重複 alert の ML の影の記録 (``alert_dedup_shadow`` テーブル、R1h、2026-10-11)。

本番の配信 (alert のまま / 降格) は変えない。配信時点の本物の特徴量で ML の判断を貯め、
Opus 正解が溜まった後に評価する。``routing_v2_shadow`` と同じ append-only + retention の形。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from src.storage.repo_base import RunHistoryRepositoryBase
from src.storage.row_mappers import _to_iso

DEFAULT_LIST_LIMIT = 50
DEFAULT_RETENTION_DAYS = 180


@dataclass(frozen=True)
class AlertDedupShadowRow:
    """影の記録 1 件 (1 記事 = alert 降格判定の直前の 1 回)。"""

    article_id: str
    prior_article_id: str
    p_suppress: float
    model_version: str
    features_json: str
    would_suppress: bool
    created_at: str = ""


def _cutoff(days: int) -> str:
    return _to_iso(datetime.now(UTC) - timedelta(days=days))


class AlertDedupShadowMixin(RunHistoryRepositoryBase):
    """``alert_dedup_shadow`` テーブルの読み書き。"""

    def record_alert_dedup_shadow(self: Any, row: AlertDedupShadowRow) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO alert_dedup_shadow (article_id, created_at, prior_article_id,"
                " p_suppress, model_version, features_json, would_suppress)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    row.article_id,
                    _to_iso(datetime.now(UTC)),
                    row.prior_article_id,
                    float(row.p_suppress),
                    row.model_version,
                    row.features_json,
                    1 if row.would_suppress else 0,
                ),
            )

    def alert_dedup_shadow_p_values(self: Any, *, days: int) -> list[tuple[float, bool]]:
        """直近 ``days`` 日の ``(p_suppress, would_suppress)`` (件数・分布の集計用)。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT p_suppress, would_suppress FROM alert_dedup_shadow WHERE created_at >= ?",
                (_cutoff(days),),
            ).fetchall()
        return [(float(r["p_suppress"]), bool(r["would_suppress"])) for r in rows]

    def list_alert_dedup_shadow(
        self: Any, *, days: int, limit: int = DEFAULT_LIST_LIMIT
    ) -> list[AlertDedupShadowRow]:
        """直近の記録を新しい順に返す (features_json 込み)。"""
        sql = (
            "SELECT article_id, prior_article_id, p_suppress, model_version, features_json,"
            " would_suppress, created_at, id FROM alert_dedup_shadow WHERE created_at >= ?"
            " ORDER BY created_at DESC, id DESC LIMIT ?"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, (_cutoff(days), int(limit))).fetchall()
        return [
            AlertDedupShadowRow(
                article_id=str(r["article_id"]),
                prior_article_id=str(r["prior_article_id"]),
                p_suppress=float(r["p_suppress"]),
                model_version=str(r["model_version"]),
                features_json=str(r["features_json"]),
                would_suppress=bool(r["would_suppress"]),
                created_at=str(r["created_at"]),
            )
            for r in rows
        ]

    def purge_alert_dedup_shadow(self: Any, days: int = DEFAULT_RETENTION_DAYS) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM alert_dedup_shadow WHERE created_at < ?", (_cutoff(days),)
            )
            return int(cur.rowcount or 0)


__all__ = [
    "DEFAULT_LIST_LIMIT",
    "DEFAULT_RETENTION_DAYS",
    "AlertDedupShadowMixin",
    "AlertDedupShadowRow",
]
