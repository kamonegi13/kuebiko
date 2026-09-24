"""要約埋込の永続化 (2026-09-21)。

⭐ **本文の埋込 (article_embeddings) とは別に持つ**。あちらは意味的重複排除でも
使っており、入れ替えると別の機能に影響する。群化には「書式の揃った要約」の埋込の
方が効く (実測 +4pt)。

⚠ 以前はシャドー観測用に**その場で作って捨てて**いた。事象どうしの統合を毎時
回すには永続化が要る — 全期間で総当たりすると 13,000 件を毎回作り直すことになり、
ML は一瞬なのに埋込生成で数十分かかっていた (2026-09-21 に実測して判明)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

import numpy as np

_CHUNK = 400


def summary_embedding_text(title: str | None, summary: str | None) -> str:
    """要約埋込の入力 (見出し + 空行 + 要約)。**形の SSoT**。

    群化・補完・台帳の割当の関門が共有する。

    ⚠ 2026-09-24 に割当の関門が「見出し + 改行 1 つ」で作っていて、保存済みの埋込と形が食い違った。
    """
    return f"{title or ''}\n\n{summary or ''}".strip()


class _Conn(Protocol):
    def _connect(self) -> Any: ...


class SummaryEmbeddingMixin:
    """``RunHistoryRepository`` に混ぜる読み書き。"""

    def load_summary_embeddings(self: Any, article_ids: Sequence[str]) -> dict[str, np.ndarray]:
        """保存済みの要約埋込を引く。**無いものは黙って欠ける** (呼び手が作る)。"""
        out: dict[str, np.ndarray] = {}
        ids = list(dict.fromkeys(article_ids))
        if not ids:
            return out
        with self._connect() as conn:
            for i in range(0, len(ids), _CHUNK):
                chunk = ids[i : i + _CHUNK]
                ph = ",".join("?" * len(chunk))
                for row in conn.execute(
                    "SELECT article_id, vector, dim FROM summary_embeddings "  # noqa: S608
                    f"WHERE article_id IN ({ph})",
                    tuple(chunk),
                ).fetchall():
                    raw = row["vector"]
                    vec = np.frombuffer(bytes(raw), dtype=np.float32)
                    if vec.size == int(row["dim"]):
                        out[str(row["article_id"])] = vec
        return out

    def summary_embedding_inputs(self: Any, article_ids: Sequence[str]) -> dict[str, str]:
        """要約埋込を作るための入力文 (``summary_embedding_text``)。記事が無ければ欠ける。"""
        out: dict[str, str] = {}
        ids = list(dict.fromkeys(article_ids))
        if not ids:
            return out
        with self._connect() as conn:
            for i in range(0, len(ids), _CHUNK):
                chunk = ids[i : i + _CHUNK]
                ph = ",".join("?" * len(chunk))
                for row in conn.execute(
                    f"SELECT article_id, title, summary FROM articles WHERE article_id IN ({ph})",  # noqa: S608
                    tuple(chunk),
                ).fetchall():
                    aid = str(row["article_id"])
                    text = summary_embedding_text(row["title"], row["summary"])
                    if text and aid not in out:  # articles は同じ id が複数行ありうる
                        out[aid] = text
        return out

    def save_summary_embeddings(self: Any, vectors: Mapping[str, np.ndarray], *, model: str) -> int:
        """要約埋込を upsert する。戻り値は書いた件数。"""
        if not vectors:
            return 0
        rows = [
            (aid, model, int(v.size), np.asarray(v, dtype=np.float32).tobytes())
            for aid, v in vectors.items()
            if v is not None and v.size
        ]
        if not rows:
            return 0
        with self._connect() as conn:
            for row in rows:
                conn.execute(
                    "INSERT INTO summary_embeddings (article_id, model, dim, vector) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT (article_id) DO UPDATE SET "
                    "model = excluded.model, dim = excluded.dim, vector = excluded.vector",
                    row,
                )
            conn.commit()
        return len(rows)
