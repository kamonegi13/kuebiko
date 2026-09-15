"""常設情報要求 (問い) の面 API — PIR ブリーフ 段A。

GET /api/v1/questions  問いごとの「いまの答え」+ 変化 + 指標 + 鮮度。

設計: docs/pir_brief_design.md §3 / docs/intelligence_requirements_layering.md。

**期間の産物ではない** — 日・週・月で区切らず、「いま時点の答え」と「前回の答えから何が
どう動いたか」を返す。窓を持たないので period の Query も持たない (これが SIR 側の
配信物との構造的な違い)。

データ源は ``build_standing_posture`` 1 本 (重要インフラ board の posture カードと同じ
射影を読む — **別集計を作らない**、docs/prepositioning_posture_ledger_design.md §5.1)。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter

questions_api = APIRouter(prefix="/api/v1/questions", tags=["questions"])

_TTL_SECONDS = 60.0
_cache: tuple[float, dict[str, Any]] | None = None

#: 「答えが動いた」と見なす delta (継続・未追跡は動いていない)。
_MOVED_DELTAS = frozenset(
    {
        "opened",
        "hypothesis_flip",
        "strengthened",
        "weakened",
        "escalated",
        "reopened",
        "claim_revised",
    }
)


def _summarize(questions: list[dict[str, Any]]) -> dict[str, Any]:
    """冒頭の 1 行 — 何問中いくつの答えが動いたか。

    静穏日に「動いていない」と明示できることが PIR ブリーフの要件
    (「静か≠安全」— 古いことでなく、古いと分からないことが危険)。
    """
    assessed = [q for q in questions if q.get("assessed")]
    moved = [q for q in assessed if str(q.get("delta_type") or "") in _MOVED_DELTAS]
    return {
        "total": len(questions),
        "assessed": len(assessed),
        "moved": len(moved),
        "unassessed": len(questions) - len(assessed),
    }


@questions_api.get("")
def list_questions() -> dict[str, Any]:
    """常設情報要求の現在の答え一覧 (窓なし・状態の射影)。"""
    global _cache
    from src.ui.services.standing_posture import build_standing_posture

    now = time.monotonic()
    if _cache is not None and now - _cache[0] < _TTL_SECONDS:
        return _cache[1]
    questions = build_standing_posture()
    payload = {"questions": questions, "summary": _summarize(questions)}
    _cache = (now, payload)
    return payload
