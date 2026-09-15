"""常設情報要求 posture カード (段C、2026-07-13)。

board の第 3 の独立レンズ: 「国家 N は日本の重要インフラへの事前配置を進めているか」の
現在推定 (リード仮説 + 較正確度) と確度の軌跡。**データ源は situations +
situation_revisions のみ** (board 集計と独立 — 「カードの確度・軌跡 = revisions と一致、
別集計を作らない」の受入基準をクエリ構造で保証)。ラダー/世界行動には折り込まない。
設計: docs/prepositioning_posture_ledger_design.md §5.1。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.assessment.situation_store import SituationStore
from src.assessment.standing import STANDING_SEEDS
from src.storage.run_history import DEFAULT_DB_PATH
from src.synthesis.grounded.hypotheses import get_hypothesis

# nation → 表示ラベル。SSoT は overview._NATION_LABELS (有機的結合監査 M3: 複製を
# 増やさない — 新規コピーでなく既存定義を参照する)。
from src.ui.services.overview import _NATION_LABELS

_TRAJECTORY_LIMIT = 12
_EVIDENCE_WINDOW_DAYS = 30
#: 「何が見えれば答えが変わるか」に載せる指標の上限 (新しい順)。
_INDICATOR_LIMIT = 8


def _json_list(raw: object) -> list[str]:
    """revision の JSON 列 → 文字列 list (壊れていれば空 = 面を落とさない)。"""
    if not raw:
        return []
    try:
        loaded = json.loads(str(raw))
    except json.JSONDecodeError:
        return []
    return [str(x) for x in loaded if str(x).strip()] if isinstance(loaded, list) else []


def build_standing_posture(
    db_path: Path = DEFAULT_DB_PATH, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """常設 4 件の posture カード payload (seed 順、未開設は除外)。

    ``now`` = 30 日証拠窓の基準時刻 (既定は実時刻)。テストは fixture の固定時刻を
    渡す — 実時刻直書きだと fixture 日付 + 30 日で失効する時限テストになる
    (2026-08-13 に実際に失効した)。
    """
    store = SituationStore(db_path=db_path)
    since = ((now or datetime.now(UTC)) - timedelta(days=_EVIDENCE_WINDOW_DAYS)).isoformat()
    cards: list[dict[str, Any]] = []
    for seed in STANDING_SEEDS:
        row = store.get_situation(seed.situation_id)
        if row is None:
            continue
        with store._repo._connect() as conn:  # noqa: SLF001 — 読み取り専用の意図的共有
            rev_rows = conn.execute(
                "SELECT rev, claim, leading_hypothesis, confidence, delta_type, created_at,"
                " delta_note, confidence_basis, missing"
                " FROM situation_revisions WHERE situation_id=?"
                " ORDER BY rev DESC LIMIT ?",
                (seed.situation_id, _TRAJECTORY_LIMIT),
            ).fetchall()
            indicator_rows = conn.execute(
                "SELECT indicator, status, opened_at, horizon_days"
                " FROM situation_forecasts WHERE situation_id=?"
                " ORDER BY opened_at DESC LIMIT ?",
                (seed.situation_id, _INDICATOR_LIMIT),
            ).fetchall()
            counts = conn.execute(
                "SELECT COUNT(*),"
                " SUM(CASE WHEN a.victim_country_iso = 'JP' THEN 1 ELSE 0 END)"
                " FROM situation_evidence e JOIN articles a ON a.article_id = e.article_id"
                " WHERE e.situation_id = ? AND datetime(a.created_at) >= datetime(?)",
                (seed.situation_id, since),
            ).fetchone()
        revs = list(reversed(rev_rows))
        latest = revs[-1] if revs else None
        leading = str(latest[2]) if latest else ""
        hyp = get_hypothesis(leading) if leading else None
        cards.append(
            {
                "situation_id": seed.situation_id,
                "nation": seed.nation,
                "nation_label": _NATION_LABELS.get(seed.nation, seed.nation),
                "question": seed.title,
                "assessed": latest is not None,
                "claim": str(latest[1]) if latest else "",
                "leading_hypothesis": leading,
                "leading_label": hyp.label if hyp else leading,
                "confidence": str(latest[3]) if latest else "",
                # 確度の根拠 (ACH=… / source_basis=…)。台帳 (situation_revisions) に既に
                # 記録されているが未露出だった — honesty doctrine: 確度は接地を可視化する。
                "confidence_basis": str(latest[7]) if latest else "",
                "delta_type": str(latest[4]) if latest else "",
                # 前回の答えから**なぜ**動いたか。問いを主語に読むとき、確度の数字より
                # 「何が変わったのでこうなったか」が要る (docs/pir_brief_design.md §3)。
                "delta_note": str(latest[6] or "") if latest else "",
                # 何が分かっていないか (答えの限界を答えと同じ面に置く = honesty doctrine)。
                "missing_evidence": _json_list(latest[8]) if latest else [],
                "assessed_at": str(latest[5]) if latest else "",
                # 鮮度: この答えがいつの証拠に基づくか。静穏な問いほど重要な欄
                # (「静か≠安全」— 古いことでなく、古いと分からないことが危険)。
                "last_evidence_at": row.last_evidence_at,
                # 何が見えれば答えが変わるか (I&W)。open=未発火 / hit=発火 / expired=期限切れ。
                "indicators": [
                    {
                        "indicator": str(i[0]),
                        "status": str(i[1]),
                        "opened_at": str(i[2]),
                        "horizon_days": int(i[3] or 0),
                    }
                    for i in indicator_rows
                ],
                "evidence_related_30d": int(counts[0] or 0),
                "evidence_direct_30d": int(counts[1] or 0),
                "trajectory": [
                    {
                        "rev": int(r[0]),
                        "at": str(r[5]),
                        "confidence": str(r[3]),
                        "delta_type": str(r[4]),
                        "note": str(r[6] or "")[:100],
                    }
                    for r in revs
                ],
            }
        )
    return cards
