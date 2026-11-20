"""状況総括の部品を束に分けて書かせる (2026-11-20、docs/synthesis_length_gate.md §2.5)。

旗 ``SYNTHESIS_CLAUSE_CHUNK=0|shadow|1`` (コードの既定 0、compose の既定 shadow)。
週次は 1 回の呼び出しで 30〜34 項目・プロンプト 3.6 万字を書かせており、学習 (n20) と日次の幅
(15 項目以下) の倍で、出力上限の 1 万トークンまで余裕が 25% しかない。書かせる判定 (変化 + 継続、
salience 順) を ``CHUNK_ITEMS`` 以下の束に**均等に**分け、束ごとに同じ v9 で書かせて
items を連結する。見出しの文と注視点は第 1 束 (salience 上位) から取る。15 件以下 (日次) は
1 束で、旗に関わらず従来と同一。

- shadow: 配信は 1 回の呼び出しの版。その後に束の版を別に作り、関門の件数をログ
  ``synthesis_clause_chunk_shadow`` に、節の全文をローカルの JSONL に残す (盲検の比較用)。
- 1: 束の版で配信する (shadow を読んでから)。
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from src.logging_config import get_logger
from src.synthesis.grounded.estimate import Estimate
from src.synthesis.grounded.render import RenderPlan, render_chunk_prompt
from src.tools.llm_client import LLMClient

if TYPE_CHECKING:
    from src.synthesis.grounded.slot_render import WireJudgmentClauses

_log = get_logger(__name__)

FLAG = "SYNTHESIS_CLAUSE_CHUNK"
#: 1 束の上限 = 日次の幅 (変化 12 + 継続 3)。学習時の分布に収める
CHUNK_ITEMS = 15
SHADOW_DIR_ENV = "SYNTHESIS_CHUNK_SHADOW_DIR"
DEFAULT_SHADOW_DIR = "/app/data/eval/synthesis_chunk_shadow"

Mode = Literal["off", "shadow", "on"]


def chunk_mode() -> Mode:
    raw = os.environ.get(FLAG, "0").strip().lower()
    if raw == "shadow":
        return "shadow"
    if raw in {"1", "on", "true"}:
        return "on"
    return "off"


def split_even(ids: Sequence[str], size: int = CHUNK_ITEMS) -> list[list[str]]:
    """順序を保って ``size`` 以下の束に均等に分ける (33 件・15 → 11/11/11)。"""
    if not ids:
        return []
    n = math.ceil(len(ids) / size)
    base, extra = divmod(len(ids), n)
    out: list[list[str]] = []
    start = 0
    for k in range(n):
        end = start + base + (1 if k < extra else 0)
        out.append(list(ids[start:end]))
        start = end
    return out


def needs_chunks(plan: RenderPlan) -> bool:
    return len(plan.moved_ids) + len(plan.standing_ids) > CHUNK_ITEMS


async def write_chunked_clauses(
    *, llm: LLMClient, est: Estimate, plan: RenderPlan, period_label: str, template: str
) -> tuple[WireJudgmentClauses, int]:
    """束ごとに部品を書かせて連結する。返り値は (部品, 束の数)。"""
    from src.synthesis.grounded.slot_render import WireJudgmentClauses, write_clauses

    moved = set(plan.moved_ids)
    chunks = split_even([*plan.moved_ids, *plan.standing_ids])
    parts: list[WireJudgmentClauses] = []
    for k, ids in enumerate(chunks):
        prompt = render_chunk_prompt(
            est=est,
            plan=plan,
            period_label=period_label,
            moved_ids=[i for i in ids if i in moved],
            standing_ids=[i for i in ids if i not in moved],
            first=k == 0,
            template=template,
        )
        parts.append(
            await write_clauses(
                llm=llm, prompt=prompt, est=est, period_label=f"{period_label} [束{k + 1}]"
            )
        )
    merged = WireJudgmentClauses(
        headline_clause=parts[0].headline_clause if parts else "",
        items=[item for part in parts for item in part.items],
        watch_clause=next((p.watch_clause for p in parts if p.watch_clause.strip()), ""),
    )
    return merged, len(chunks)


def _append_shadow_row(row: dict[str, object], now: datetime) -> None:
    try:
        directory = Path(os.environ.get(SHADOW_DIR_ENV, DEFAULT_SHADOW_DIR))
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{now:%Y-%m}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        _log.warning("synthesis_clause_chunk_shadow_write_failed", error_type=type(e).__name__)


async def shadow_chunked(
    *,
    llm: LLMClient,
    est: Estimate,
    plan: RenderPlan,
    period_label: str,
    template: str,
    delivered_stats: dict[str, int],
) -> None:
    """shadow: 配信の後に束の版を作り、件数をログに・節を JSONL に残す。失敗は握る。"""
    from src.synthesis.grounded.slot_render import assemble_sections

    if not needs_chunks(plan):
        return
    try:
        clauses, n_chunks = await write_chunked_clauses(
            llm=llm, est=est, plan=plan, period_label=period_label, template=template
        )
        sections, stats = assemble_sections(clauses, est=est, plan=plan, fit=True)
    except Exception as e:  # noqa: BLE001 — shadow は配信に影響させない
        _log.warning("synthesis_clause_chunk_shadow_failed", error=str(e)[:160])
        return
    now = datetime.now(UTC)
    _log.info(
        "synthesis_clause_chunk_shadow",
        period=period_label,
        chunks=n_chunks,
        items=len(clauses.items),
        delivered=delivered_stats,
        **stats.__dict__,
    )
    _append_shadow_row(
        {
            "recorded_at": now.isoformat(),
            "period_type": est.period_type,
            "period_label": period_label,
            "period_start": est.period_start.isoformat(),
            "model": getattr(llm, "model", ""),
            "chunks": n_chunks,
            "delivered_stats": delivered_stats,
            "chunked_stats": stats.__dict__,
            "sections": sections.model_dump(),
        },
        now,
    )


__all__ = [
    "CHUNK_ITEMS",
    "FLAG",
    "chunk_mode",
    "needs_chunks",
    "shadow_chunked",
    "split_even",
    "write_chunked_clauses",
]
