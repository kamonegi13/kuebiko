"""深刻度の軸の永続化 (2026-09-25、``src/cti/severity_axes.py``)。

記事ごとに 1 回だけ付ける (``INSERT OR IGNORE``)。付けたモデルを残す — detect ML は軸を付けた
モデルの癖ごと学習するので、モデルが替わったことを後から見分けられるようにする。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from src.cti.severity_axes import STORED_FIELDS

_CHUNK = 400


class SeverityAxesMixin:
    """``RunHistoryRepository`` に混ぜる読み書き。"""

    def get_severity_axes(self: Any, article_ids: Sequence[str]) -> dict[str, dict[str, str]]:
        """保存済みの軸を引く (無い記事は返さない)。値は欄名 → 選択肢 (+ ``model``)。"""
        ids = list(dict.fromkeys(article_ids))
        out: dict[str, dict[str, str]] = {}
        cols = ", ".join((*STORED_FIELDS, "model"))
        with self._connect() as conn:
            for i in range(0, len(ids), _CHUNK):
                chunk = ids[i : i + _CHUNK]
                ph = ",".join("?" * len(chunk))
                for r in conn.execute(
                    f"SELECT article_id, {cols} FROM article_severity_axes "  # noqa: S608
                    f"WHERE article_id IN ({ph})",
                    tuple(chunk),
                ).fetchall():
                    row = dict(r)
                    out[str(row["article_id"])] = {
                        k: str(row[k]) for k in (*STORED_FIELDS, "model")
                    }
        return out

    def set_severity_axes(self: Any, article_id: str, axes: Mapping[str, str], model: str) -> None:
        """軸を記録する (冪等 — 既存は上書きしない)。"""
        cols = (*STORED_FIELDS, "model", "created_at")
        values = (
            *(str(axes[k]) for k in STORED_FIELDS),
            model,
            datetime.now(UTC).isoformat(),
        )
        with self._connect() as conn:
            conn.execute(
                f"INSERT OR IGNORE INTO article_severity_axes (article_id, {', '.join(cols)}) "  # noqa: S608
                f"VALUES (?, {', '.join('?' * len(cols))})",
                (article_id, *values),
            )
