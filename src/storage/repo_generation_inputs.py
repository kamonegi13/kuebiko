"""生成物の入力 (generation_inputs テーブル、P0-18)。

物語系の生成物の LLM に渡した prompt 本文を残す。**本文を含む**ためローカル専用 (CLAUDE.md §9)。
年齢による削除は置かない (利用者判断)。書き手は ``src.ops.generation_inputs`` 経由。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from src.storage.repo_base import RunHistoryRepositoryBase


class GenerationInputsMixin(RunHistoryRepositoryBase):
    """generation_inputs テーブルの読み書き。"""

    def record_generation_input(
        self,
        *,
        step: str,
        model: str,
        product_kind: str,
        product_id: str,
        prompt_sha256: str,
        prompt_text: str,
        input_json: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO generation_inputs (recorded_at, step, model, product_kind,"
                " product_id, prompt_sha256, prompt_chars, prompt_text, input_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(UTC).isoformat(),
                    step[:80],
                    model[:120],
                    product_kind[:40],
                    product_id[:200],
                    prompt_sha256,
                    len(prompt_text),
                    prompt_text,
                    input_json,
                ),
            )

    def generation_inputs_counts(self, *, days: int) -> dict[str, int]:
        """直近 ``days`` 日の product_kind 別の件数 (本文は返さない)。"""
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            rows: list[Any] = conn.execute(
                "SELECT product_kind, COUNT(*) AS n FROM generation_inputs"
                " WHERE recorded_at >= ? GROUP BY product_kind",
                (since,),
            ).fetchall()
        return {str(r["product_kind"]): int(r["n"]) for r in rows}
