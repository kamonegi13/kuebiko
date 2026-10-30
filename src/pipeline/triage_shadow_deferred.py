"""triage の影の記録を run 末尾で回す (2026-10-31)。

``src.tools.triage_shadow`` の影の記録 (平たい triage + 取り込みヒントの並走、最大 20 件) は
LLM を呼ぶが本番の出力に効かない。以前は記事ループの**前**で回して約 28 秒/run を食い、
ループの時間枠 (soft deadline) を削っていた。ここでは記事処理・永続化の**後**に回し、
親の wallclock timeout までの残り時間で絞る。残りが少なければ**飛ばす** (記録が欠けるだけ。
本番の出力・親の timeout には影響しない)。記録の中身は従来と同じ。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Protocol

from src.logging_config import get_logger
from src.storage.repo_triage_shadow import TriageShadowRow
from src.tools.article_model import Article
from src.tools.embedding_client import EmbeddingClient
from src.tools.llm_client import LLMClient
from src.tools.triage_shadow import run_triage_shadow, shadow_sample_size

_log = get_logger(__name__)

#: 親の timeout までの残りから常に空けておく余白 (後続の通知・合成の起動・子の終了処理)
SHADOW_SAFETY_MARGIN_SECONDS = 60.0
#: 使える時間がこれ未満なら影の記録を飛ばす (20 件を 3 並列で回すと約 28 秒)
SHADOW_MIN_USABLE_SECONDS = 30.0


class _ShadowRepo(Protocol):
    def record_triage_shadow(self, rows: Sequence[TriageShadowRow]) -> int: ...


def remaining_budget_seconds(hard_deadline: float | None) -> float | None:
    """親の timeout までの残り秒 (monotonic)。予算未指定 (None) は無制限として None。"""
    if hard_deadline is None:
        return None
    return hard_deadline - time.monotonic()


async def run_deferred_triage_shadow(
    decisions: Sequence[tuple[Article, str, bool]],
    *,
    llm: LLMClient,
    keep_importance: set[str],
    think: bool,
    relevance_embedder: EmbeddingClient | None,
    relevance_cascade_llm: LLMClient | None,
    repo: _ShadowRepo | None,
    remaining_seconds: float | None,
) -> int:
    """後ろ倒しした影の記録を回して保存する。戻り値は保存した行数 (飛ばしたら 0)。

    失敗・時間切れは log だけで握る (呼び出し側の run を壊さない)。
    """
    requested = min(len(decisions), shadow_sample_size())
    if requested <= 0 or repo is None:
        return 0
    usable: float | None = None
    if remaining_seconds is not None:
        usable = remaining_seconds - SHADOW_SAFETY_MARGIN_SECONDS
        if usable < SHADOW_MIN_USABLE_SECONDS:
            _log.info(
                "triage_shadow_deferred_skipped",
                reason="low_budget",
                skipped=requested,
                remaining_seconds=round(remaining_seconds, 1),
            )
            return 0
    try:
        rows = await asyncio.wait_for(
            run_triage_shadow(
                decisions,
                llm=llm,
                keep_importance=keep_importance,
                think=think,
                relevance_embedder=relevance_embedder,
                relevance_cascade_llm=relevance_cascade_llm,
                budget_seconds=usable,
            ),
            timeout=usable,
        )
    except TimeoutError:
        _log.warning("triage_shadow_deferred_skipped", reason="timeout", skipped=requested)
        return 0
    except Exception as e:  # noqa: BLE001 — 影の記録の失敗は run を止めない
        _log.warning("triage_shadow_run_failed", error=str(e)[:200])
        return 0
    if not rows:
        return 0
    try:
        repo.record_triage_shadow(rows)
    except Exception as e:  # noqa: BLE001
        _log.warning("triage_shadow_record_failed", error=str(e)[:200])
        return 0
    _log.info(
        "triage_shadow_deferred_done",
        requested=requested,
        recorded=len(rows),
        skipped=requested - len(rows),
    )
    return len(rows)
