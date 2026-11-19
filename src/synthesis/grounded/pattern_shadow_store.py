"""状況総括パターン版の shadow の出力をローカルの JSONL に残す (旗 SYNTHESIS_PATTERNS=shadow)。

5 回目の判定 (docs/synthesis_pattern_redesign.md) は新旧の文を盲検で比べるため、件数の log
だけでは足りない。評価専用・期間限定の記録なので DB の表は作らず、data/ (gitignore・
ローカルのみ) の月ごとの JSONL に 1 期間 1 行で追記する。本番の節 (status_synthesis) とは
``period_type`` + ``period_start`` で突き合わせる。書き込みの失敗は log だけ (配信は不変)。
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.logging_config import get_logger
from src.synthesis.grounded.estimate import Estimate
from src.synthesis.grounded.pattern_gates import GateStats
from src.synthesis.grounded.patterns import PatternPlan
from src.synthesis.grounded.render import _WireSections

_log = get_logger(__name__)

SHADOW_DIR_ENV = "SYNTHESIS_PATTERNS_SHADOW_DIR"
DEFAULT_SHADOW_DIR = "/app/data/eval/synthesis_patterns_shadow"


def shadow_dir() -> Path:
    return Path(os.environ.get(SHADOW_DIR_ENV, DEFAULT_SHADOW_DIR))


def build_shadow_row(
    *,
    est: Estimate,
    period_label: str,
    model: str,
    plan: PatternPlan,
    stats: GateStats | None,
    sections: _WireSections,
    recorded_at: datetime,
) -> dict[str, Any]:
    """1 期間の shadow の記録 (純粋関数)。"""
    return {
        "recorded_at": recorded_at.isoformat(),
        "period_type": est.period_type,
        "period_label": period_label,
        "period_start": est.period_start.isoformat(),
        "model": model,
        "judgments": len(est.judgments),
        "patterns": len(plan.patterns),
        "pattern_sizes": [p.count for p in plan.patterns],
        "notable": len(plan.notable),
        "rest": len(plan.rest),
        "moved": plan.moved_count,
        "links": [
            {"a": link.a.title, "b": link.b.title, "shared": list(link.shared)}
            for link in plan.links
        ],
        "headline_extra": plan.headline_extra.title if plan.headline_extra else None,
        "gate_stats": stats.__dict__ if stats else None,
        "sections": sections.model_dump(),
    }


def append_shadow_row(
    *,
    est: Estimate,
    period_label: str,
    model: str,
    plan: PatternPlan,
    stats: GateStats | None,
    sections: _WireSections,
    now: datetime | None = None,
) -> Path | None:
    """月ごとの JSONL に 1 行追記する。失敗は None (log のみ)。"""
    at = now or datetime.now(UTC)
    try:
        row = build_shadow_row(
            est=est,
            period_label=period_label,
            model=model,
            plan=plan,
            stats=stats,
            sections=sections,
            recorded_at=at,
        )
        out_dir = shadow_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{at.strftime('%Y-%m')}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001 — shadow の記録は配信に影響させない
        _log.warning("synthesis_pattern_shadow_write_failed", error_type=type(e).__name__)
        return None
    _log.info("synthesis_pattern_shadow_written", period=period_label, file=path.name)
    return path


__all__ = [
    "DEFAULT_SHADOW_DIR",
    "SHADOW_DIR_ENV",
    "append_shadow_row",
    "build_shadow_row",
    "shadow_dir",
]
