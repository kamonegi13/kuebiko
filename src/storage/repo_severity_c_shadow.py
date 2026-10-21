# ruff: noqa: E501  (日本語の文は幅 2 で数えられ長く見える)
"""深刻さ C の影の記録 (``severity_c_shadow`` テーブル、2026-10-22)。

本番の配信・重要度は変えない。``alert_dedup_shadow`` と同じ append-only + retention の形で、
1 記事 1 行 (article_id UNIQUE、再記録は無視)。A (決まりごと) と C (問い + ML) を並べて評価する材料。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from src.storage.repo_base import RunHistoryRepositoryBase
from src.storage.row_mappers import _to_iso

DEFAULT_RETENTION_DAYS = 60
DEFAULT_LIST_LIMIT = 50


@dataclass(frozen=True)
class SeverityCShadowRow:
    """影の記録 1 件。``skip_reason`` ありは問いを当てていない (p_s3 等は None)。"""

    article_id: str
    skip_reason: str | None = None
    p_s3: float | None = None
    would_s3: bool | None = None
    threshold: float | None = None
    probe_model: str | None = None
    trained_model: str | None = None
    model_match: bool | None = None
    probs_json: str | None = None
    a_severity: str | None = None
    a_basis: str | None = None
    created_at: str = ""


def _cutoff(days: int) -> str:
    return _to_iso(datetime.now(UTC) - timedelta(days=days))


def _flag(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _opt_bool(v: Any) -> bool | None:
    return None if v is None else bool(v)


def _opt_float(v: Any) -> float | None:
    return None if v is None else float(v)


_COLUMNS = (
    "article_id, created_at, skip_reason, p_s3, would_s3, threshold, probe_model,"
    " trained_model, model_match, probs_json, a_severity, a_basis"
)


def _row(r: Any) -> SeverityCShadowRow:
    return SeverityCShadowRow(
        article_id=str(r["article_id"]),
        skip_reason=r["skip_reason"],
        p_s3=_opt_float(r["p_s3"]),
        would_s3=_opt_bool(r["would_s3"]),
        threshold=_opt_float(r["threshold"]),
        probe_model=r["probe_model"],
        trained_model=r["trained_model"],
        model_match=_opt_bool(r["model_match"]),
        probs_json=r["probs_json"],
        a_severity=r["a_severity"],
        a_basis=r["a_basis"],
        created_at=str(r["created_at"]),
    )


class SeverityCShadowMixin(RunHistoryRepositoryBase):
    """``severity_c_shadow`` テーブルの読み書き。"""

    def record_severity_c_shadow(self: Any, row: SeverityCShadowRow) -> None:
        """1 記事 1 行。すでにあれば何もしない (既存行を優先)。"""
        with self._connect() as conn:
            if conn.execute(
                "SELECT 1 FROM severity_c_shadow WHERE article_id = ?", (row.article_id,)
            ).fetchone():
                return
            conn.execute(
                f"INSERT INTO severity_c_shadow ({_COLUMNS}) "  # noqa: S608 — 列名は定数
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row.article_id,
                    _to_iso(datetime.now(UTC)),
                    row.skip_reason,
                    row.p_s3,
                    _flag(row.would_s3),
                    row.threshold,
                    row.probe_model,
                    row.trained_model,
                    _flag(row.model_match),
                    row.probs_json,
                    row.a_severity,
                    row.a_basis,
                ),
            )

    def list_severity_c_shadow(
        self: Any, *, days: int, limit: int | None = None
    ) -> list[SeverityCShadowRow]:
        """直近 ``days`` 日の記録を新しい順に (``limit`` なしは全件、集計用)。"""
        sql = (
            f"SELECT {_COLUMNS}, id FROM severity_c_shadow "  # noqa: S608 — 列名は定数
            "WHERE created_at >= ? ORDER BY created_at DESC, id DESC"
        )
        params: tuple[Any, ...] = (_cutoff(days),)
        if limit is not None:
            sql += " LIMIT ?"
            params = (*params, int(limit))
        with self._connect() as conn:
            return [_row(r) for r in conn.execute(sql, params).fetchall()]

    def purge_severity_c_shadow(self: Any, days: int = DEFAULT_RETENTION_DAYS) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM severity_c_shadow WHERE created_at < ?", (_cutoff(days),)
            )
            return int(cur.rowcount or 0)


__all__ = [
    "DEFAULT_LIST_LIMIT",
    "DEFAULT_RETENTION_DAYS",
    "SeverityCShadowMixin",
    "SeverityCShadowRow",
]
