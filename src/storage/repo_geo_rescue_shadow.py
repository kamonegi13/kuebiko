# ruff: noqa: E501  (日本語の文は幅 2 で数えられ長く見える)
"""地政学の救済の判定 v2 の影の記録 (``geo_rescue_shadow`` テーブル、2026-10-31)。

本番の救済 (flat) は変えない。救済の候補 1 件 = 1 行で、flat (本番の判定)・pir・語の関門の手掛かりの
種類・QW/QC/QD の確率・v2 の最終判定を並べて残す。後で盲検の標本と比べる材料
(事前登録 ``docs/replay_prereg/geo_rescue_shadow.md``)。``alert_dedup_shadow`` と同じ append-only +
retention の形。**本文・題は持たない** (判定の入力は種類と sha256 だけ)。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from src.storage.repo_base import RunHistoryRepositoryBase
from src.storage.row_mappers import _to_iso

DEFAULT_RETENTION_DAYS = 180
DEFAULT_LIST_LIMIT = 50

#: 行の状態。``ok`` 以外は v2 の判定が無い (``judged`` は None)
STATUS_OK = "ok"
STATUS_CAP = "cap_skipped"  # 1 回の上限の件数を超えた
STATUS_BUDGET = "budget_skipped"  # 時間の予算を使い切った
STATUS_ERROR = "error"  # pir の判定か問いが読めなかった (判定は flat の側に倒す)
STATUSES = (STATUS_OK, STATUS_CAP, STATUS_BUDGET, STATUS_ERROR)


@dataclass(frozen=True)
class GeoRescueShadowRow:
    """影の記録 1 件 (救済の候補 1 件)。"""

    article_id: str
    flat: str  # 本番の救済の判定 (high/medium/low/error)
    status: str
    pir: str | None = None  # pir の判定 (high/medium/low/error)。問わなかったら None
    cue_types: tuple[str, ...] = ()  # 語の関門の手掛かりの種類 (nation / theatre / cyber_geo 等)
    p_qw: float | None = None
    p_qc: float | None = None
    p_qd: float | None = None
    judged: bool | None = None  # v2 の最終判定 (status=ok のときだけ)
    model: str | None = None
    model_digest: str | None = None
    flat_error: bool = False
    pir_error: bool = False
    judge_failed: bool = False
    input_kind: str | None = None  # body / summary (判定の入力の種類)
    input_sha256: str | None = None  # 題 + 本文プレビューの sha256 (正解付けの入力の照合用)
    created_at: str = ""


def _cutoff(days: int) -> str:
    return _to_iso(datetime.now(UTC) - timedelta(days=days))


def _flag(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _opt_float(v: Any) -> float | None:
    return None if v is None else float(v)


def _opt_bool(v: Any) -> bool | None:
    return None if v is None else bool(v)


_COLUMNS = (
    "article_id, created_at, flat, pir, status, cue_types, p_qw, p_qc, p_qd, judged, model,"
    " model_digest, flat_error, pir_error, judge_failed, input_kind, input_sha256"
)


def _to_row(r: Any) -> GeoRescueShadowRow:
    types = str(r["cue_types"] or "")
    return GeoRescueShadowRow(
        article_id=str(r["article_id"]),
        created_at=str(r["created_at"]),
        flat=str(r["flat"]),
        pir=None if r["pir"] is None else str(r["pir"]),
        status=str(r["status"]),
        cue_types=tuple(t for t in types.split(",") if t),
        p_qw=_opt_float(r["p_qw"]),
        p_qc=_opt_float(r["p_qc"]),
        p_qd=_opt_float(r["p_qd"]),
        judged=_opt_bool(r["judged"]),
        model=None if r["model"] is None else str(r["model"]),
        model_digest=None if r["model_digest"] is None else str(r["model_digest"]),
        flat_error=bool(r["flat_error"]),
        pir_error=bool(r["pir_error"]),
        judge_failed=bool(r["judge_failed"]),
        input_kind=None if r["input_kind"] is None else str(r["input_kind"]),
        input_sha256=None if r["input_sha256"] is None else str(r["input_sha256"]),
    )


class GeoRescueShadowMixin(RunHistoryRepositoryBase):
    """``geo_rescue_shadow`` テーブルの読み書き。"""

    def record_geo_rescue_shadow(self: Any, rows: Sequence[GeoRescueShadowRow]) -> int:
        """影の記録をまとめて追記する (append-only)。空入力は 0。"""
        if not rows:
            return 0
        ts = _to_iso(datetime.now(UTC))
        values = [
            (
                r.article_id,
                ts,
                r.flat,
                r.pir,
                r.status,
                ",".join(r.cue_types),
                r.p_qw,
                r.p_qc,
                r.p_qd,
                _flag(r.judged),
                r.model,
                r.model_digest,
                int(r.flat_error),
                int(r.pir_error),
                int(r.judge_failed),
                r.input_kind,
                r.input_sha256,
            )
            for r in rows
        ]
        marks = ", ".join("?" for _ in range(17))
        with self._connect() as conn:
            conn.executemany(f"INSERT INTO geo_rescue_shadow ({_COLUMNS}) VALUES ({marks})", values)
        return len(values)

    def list_geo_rescue_shadow(self: Any, *, days: int) -> list[GeoRescueShadowRow]:
        """直近 ``days`` 日の記録を古い順に返す (集計・標本の枠に使う)。"""
        sql = (
            f"SELECT {_COLUMNS} FROM geo_rescue_shadow WHERE created_at >= ?"
            " ORDER BY created_at, id"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, (_cutoff(days),)).fetchall()
        return [_to_row(r) for r in rows]

    def purge_geo_rescue_shadow(self: Any, days: int = DEFAULT_RETENTION_DAYS) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM geo_rescue_shadow WHERE created_at < ?", (_cutoff(days),)
            )
            return int(cur.rowcount or 0)


__all__ = [
    "DEFAULT_LIST_LIMIT",
    "DEFAULT_RETENTION_DAYS",
    "STATUSES",
    "STATUS_BUDGET",
    "STATUS_CAP",
    "STATUS_ERROR",
    "STATUS_OK",
    "GeoRescueShadowMixin",
    "GeoRescueShadowRow",
]
