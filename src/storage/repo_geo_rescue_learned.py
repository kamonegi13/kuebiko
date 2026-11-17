# ruff: noqa: E501  (日本語の文は幅 2 で数えられ長く見える)
"""地政学の救済の学習版の影の記録 (``geo_rescue_learned_shadow`` テーブル、2026-11-17)。

``GEO_RESCUE_LEARNED=shadow`` のとき、毎時の判定の段が候補ごとに ML の確率・判定と本番の v2 の判定を
1 行に残す (記事ごとに 1 行)。**本文・題は持たない** (入力は ``geo_rescue_shadow`` 側)。本番の救済は不変。
retention は ``geo_rescue_shadow`` と同じ 180 日 (``purge_geo_rescue_shadow`` が一緒に消す)。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from src.storage.repo_base import RunHistoryRepositoryBase
from src.storage.row_mappers import _to_iso


@dataclass(frozen=True)
class GeoRescueLearnedRow:
    """学習版の判定 1 件。"""

    article_id: str
    p_ml: float
    ml_rescued: bool
    v2_judged: bool | None  # 本番の v2 の判定 (判定が読めなかった行は None)
    flat: str
    pir: str | None
    threshold: float
    probe_model: str | None  # 問いに使ったモデル
    probe_model_match: bool  # 学習時の問いのモデルと同じか (違えば確率は参考値)
    id: int | None = None
    created_at: str = ""


_COLUMNS = (
    "article_id, created_at, p_ml, ml_rescued, v2_judged, flat, pir, threshold,"
    " probe_model, probe_model_match"
)


def _opt_bool(v: Any) -> bool | None:
    return None if v is None else bool(v)


def _to_row(r: Any) -> GeoRescueLearnedRow:
    return GeoRescueLearnedRow(
        id=int(r["id"]),
        article_id=str(r["article_id"]),
        created_at=str(r["created_at"]),
        p_ml=float(r["p_ml"]),
        ml_rescued=bool(r["ml_rescued"]),
        v2_judged=_opt_bool(r["v2_judged"]),
        flat=str(r["flat"]),
        pir=None if r["pir"] is None else str(r["pir"]),
        threshold=float(r["threshold"]),
        probe_model=None if r["probe_model"] is None else str(r["probe_model"]),
        probe_model_match=bool(r["probe_model_match"]),
    )


class GeoRescueLearnedMixin(RunHistoryRepositoryBase):
    """``geo_rescue_learned_shadow`` テーブルの読み書き。"""

    def record_geo_rescue_learned(self: Any, rows: Sequence[GeoRescueLearnedRow]) -> int:
        """追記する (記事ごとに 1 行。既にあれば書かない)。空入力は 0。"""
        if not rows:
            return 0
        ts = _to_iso(datetime.now(UTC))
        values = [
            (
                r.article_id,
                ts,
                r.p_ml,
                int(r.ml_rescued),
                None if r.v2_judged is None else int(r.v2_judged),
                r.flat,
                r.pir,
                r.threshold,
                r.probe_model,
                int(r.probe_model_match),
            )
            for r in rows
        ]
        marks = ", ".join("?" for _ in range(len(values[0])))
        with self._connect() as conn:
            conn.executemany(
                f"INSERT INTO geo_rescue_learned_shadow ({_COLUMNS}) VALUES ({marks})"
                " ON CONFLICT(article_id) DO NOTHING",
                values,
            )
        return len(values)

    def list_geo_rescue_learned_shadow(self: Any, *, days: int) -> list[GeoRescueLearnedRow]:
        """直近 ``days`` 日の記録を古い順に返す。"""
        cutoff = _to_iso(datetime.now(UTC) - timedelta(days=days))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT id, {_COLUMNS} FROM geo_rescue_learned_shadow WHERE created_at >= ?"
                " ORDER BY created_at, id",
                (cutoff,),
            ).fetchall()
        return [_to_row(r) for r in rows]

    def purge_geo_rescue_learned_shadow(self: Any, days: int) -> int:
        cutoff = _to_iso(datetime.now(UTC) - timedelta(days=days))
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM geo_rescue_learned_shadow WHERE created_at < ?", (cutoff,)
            )
            return int(cur.rowcount or 0)


__all__ = ["GeoRescueLearnedMixin", "GeoRescueLearnedRow"]
