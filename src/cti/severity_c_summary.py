"""深刻さ C の影の記録の集計 (純粋関数、2026-10-22)。

A (決まりごと = ``derive_severity``) と C (問い + RF) を並べる。切替の判断材料は
「A が S3 と言わず C だけが S3 と言う記事」「A が S3 と言うのに C が S3 と言わない記事」の中身。
ラベル (S1〜S3・根拠名) は ``importance_v2`` の SSoT をそのまま使う。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Any

from src.storage.repo_severity_c_shadow import SeverityCShadowRow

P_HISTOGRAM_BINS = 10
#: A が深刻さを付けなかった記事のラベル (S1〜S3 と並べるための名前)
A_NONE = "none"


def _a_label(row: SeverityCShadowRow) -> str:
    return row.a_severity or A_NONE


def _histogram(values: Sequence[float]) -> list[int]:
    bins = [0] * P_HISTOGRAM_BINS
    for v in values:
        bins[min(P_HISTOGRAM_BINS - 1, int(v * P_HISTOGRAM_BINS))] += 1
    return bins


def summarize(rows: Sequence[SeverityCShadowRow], *, days: int, limit: int) -> dict[str, Any]:
    """記録 (新しい順) → API の応答。``limit`` は食い違い一覧の件数。"""
    scored = [r for r in rows if r.skip_reason is None and r.p_s3 is not None]
    skipped = Counter(r.skip_reason for r in rows if r.skip_reason)
    crosstab = Counter((_a_label(r), bool(r.would_s3)) for r in scored)
    disagreements = [r for r in scored if (r.a_severity == "S3") != bool(r.would_s3)]
    return {
        "days": days,
        "total": len(rows),
        "scored": len(scored),
        "skipped": dict(sorted(skipped.items())),
        "crosstab": [
            {"a_severity": a, "c_s3": c, "count": n} for (a, c), n in sorted(crosstab.items())
        ],
        "model_mismatch": sum(1 for r in scored if r.model_match is False),
        "probe_models": dict(Counter(r.probe_model for r in scored if r.probe_model)),
        "p_histogram": _histogram([float(r.p_s3) for r in scored if r.p_s3 is not None]),
        "disagreements": [
            {
                "article_id": r.article_id,
                "a_severity": r.a_severity,
                "a_basis": r.a_basis,
                "p_s3": r.p_s3,
                "c_s3": bool(r.would_s3),
                "probe_model": r.probe_model,
                "created_at": r.created_at,
            }
            for r in disagreements[:limit]
        ],
    }
