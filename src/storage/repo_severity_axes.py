"""深刻度の軸の永続化 (2026-09-25、``src/cti/severity_axes.py``)。

記事ごとに 1 回だけ付ける (``INSERT OR IGNORE``)。付けたモデルを残す — detect ML は軸を付けた
モデルの癖ごと学習するので、モデルが替わったことを後から見分けられるようにする。

s23 (2026-10-08) で本文から付ける 5 欄 (``EXTRA_FIELDS``) を追加。欠測は DB に NULL で保存する
(false に既定すると消費者が「無い」と読んでしまう)。真偽値は DB では INTEGER (0/1/NULL)、
呼び手には既存 7 欄と同じ ``dict[str, str]`` (値は "true"/"false") で返す — 混在させない。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from src.cti.severity_axes import BOOL_EXTRA_FIELDS, EXTRA_FIELDS, STORED_FIELDS

_CHUNK = 400


def _from_db(key: str, value: object) -> str | None:
    """DB の生値 → 呼び手向けの文字列 (欠測は None のまま、呼び手は辞書に含めない)。"""
    if value is None:
        return None
    if key in BOOL_EXTRA_FIELDS:
        return "true" if bool(value) else "false"
    return str(value)


def _to_db(key: str, value: object) -> int | str | None:
    """``axes`` (model_dump 等) の値 → DB へ書く値。None はそのまま NULL。"""
    if value is None:
        return None
    if key in BOOL_EXTRA_FIELDS:
        return 1 if value else 0
    return str(value)


class SeverityAxesMixin:
    """``RunHistoryRepository`` に混ぜる読み書き。"""

    def get_severity_axes(self: Any, article_ids: Sequence[str]) -> dict[str, dict[str, str]]:
        """保存済みの軸を引く (無い記事は返さない)。値は欄名 → 選択肢 (+ ``model``)。

        s23 の 5 欄は付いている記事だけキーを持つ (NULL は辞書から省く — ``.get(k)`` が
        自然に None を返し、「false」と取り違えない)。
        """
        ids = list(dict.fromkeys(article_ids))
        out: dict[str, dict[str, str]] = {}
        cols = ", ".join((*STORED_FIELDS, *EXTRA_FIELDS, "model"))
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
                    values = {k: str(row[k]) for k in (*STORED_FIELDS, "model")}
                    for k in EXTRA_FIELDS:
                        v = _from_db(k, row[k])
                        if v is not None:
                            values[k] = v
                    out[str(row["article_id"])] = values
        return out

    def set_severity_axes(self: Any, article_id: str, axes: Mapping[str, Any], model: str) -> None:
        """軸を記録する (冪等 — 既存は上書きしない)。

        ``axes`` は ``SeverityAxes.model_dump()`` 相当 (s23 の 5 欄は本文入力時のみ埋まり、
        それ以外は None)。
        """
        cols = (*STORED_FIELDS, *EXTRA_FIELDS, "model", "created_at")
        values = (
            *(str(axes[k]) for k in STORED_FIELDS),
            *(_to_db(k, axes.get(k)) for k in EXTRA_FIELDS),
            model,
            datetime.now(UTC).isoformat(),
        )
        with self._connect() as conn:
            conn.execute(
                f"INSERT OR IGNORE INTO article_severity_axes (article_id, {', '.join(cols)}) "  # noqa: S608
                f"VALUES (?, {', '.join('?' * len(cols))})",
                (article_id, *values),
            )
