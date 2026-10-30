"""地政学の救済の判定 v2 の影の記録の集計 (``GET /api/v1/geo-rescue-shadow/summary``、2026-10-31)。

純関数。状態別・判定別の件数、手掛かりの有無、モデルの digest の種類を出す。記事 id は出さない
(標本の抽出は事前登録の手順で DB から直接行う)。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from src.pipeline.geo_rescue_shadow import KEEP
from src.storage.repo_geo_rescue_shadow import STATUS_OK, STATUSES, GeoRescueShadowRow

#: 判定の組の名前 (flat の救済 / pir の救済 / v2 の判定)
DECISION_KEYS = (
    "flat_rescued",
    "pir_only_judged_yes",
    "pir_only_judged_no",
    "pir_no",
)


def _decision(r: GeoRescueShadowRow) -> str:
    if r.flat in KEEP:
        return "flat_rescued"
    if r.pir in KEEP:
        return "pir_only_judged_yes" if r.judged else "pir_only_judged_no"
    return "pir_no"


def summarize_geo_rescue_shadow(rows: list[GeoRescueShadowRow], *, days: int) -> dict[str, Any]:
    ok = [r for r in rows if r.status == STATUS_OK]
    by_status = Counter(r.status for r in rows)
    cue_kinds: Counter[str] = Counter()
    for r in ok:
        cue_kinds.update(r.cue_types or ("none",))
    digests = Counter((r.model or "", r.model_digest or "") for r in rows)
    return {
        "days": days,
        "total": len(rows),
        "by_status": {s: by_status.get(s, 0) for s in STATUSES},
        "by_decision": dict(Counter(_decision(r) for r in ok)),
        "judged_yes": sum(1 for r in ok if r.judged),
        "judged_no": sum(1 for r in ok if r.judged is False),
        "judge_failed": sum(1 for r in rows if r.judge_failed),
        "error_rate": round(by_status.get("error", 0) / len(rows), 4) if rows else None,
        "cue_kinds": dict(cue_kinds),
        "input_kind": dict(Counter(r.input_kind or "?" for r in ok)),
        "models": [
            {"model": m, "digest": d[:12] if d else None, "rows": n}
            for (m, d), n in sorted(digests.items())
        ],
    }


__all__ = ["DECISION_KEYS", "summarize_geo_rescue_shadow"]
