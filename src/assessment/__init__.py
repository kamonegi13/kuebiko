"""Assessment: synthesis ファミリーが共有する「現在の見立て」の横断状態。

出力中心 (daily/weekly/monthly synthesis・spotlight・recap が各々独立に context を
再導出する) から状態中心 (横断状態を一度計算し、各サーフェスは射影として読む) への
第一歩。設計: docs/synthesis_assessment_architecture.md。
"""

from __future__ import annotations

from src.assessment.context import (
    AssessmentContext,
    build_assessment_context,
    build_forecast_indicators,
    build_freshness,
    build_nation_correlation,
)

__all__ = [
    "AssessmentContext",
    "build_assessment_context",
    "build_forecast_indicators",
    "build_freshness",
    "build_nation_correlation",
]
