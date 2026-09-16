"""起草された問いの保存と昇格 — 単発 (RFI) → 常設情報要求。

設計 docs/pir_brief_design.md §6c/§6e。

**置き場は config_store** (key=``standing_questions``)。理由:
- 問いは運用者が書く**運用設定**であり、本プロジェクトの運用 config は DB が正で
  版履歴 + revert を持つ (docs: operational_config_db)。新テーブルを足さずにそれを得る
- §6e「問いはデータ」— コード所有の seed にしない

⚠ **`situations.domain` に型 (frame_id) を載せない**。domain は既に「話題の領域」
(cyber_incident / geopolitical …) を表しており、型を重ねると語の意味が二重になる。
本プロジェクトは「standing」の多重定義 (常設 4 問 / render の no_change バケツ) で
既に読み違いを起こしている。

⚠ **昇格しただけでは答えは出ない**。証拠の収穫 (``standing.harvest_standing_evidence``)
はまだ現行 4 seed の R1-R3 に固定されており、昇格した問いは収穫対象に入らない。
宣言条件で収穫する seam は段B-3c。それまで昇格した問いは「器はあるが証拠が来ない」。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Protocol

from src.assessment.question_draft import QuestionDraft, qualify, question_text
from src.logging_config import get_logger
from src.synthesis.grounded.hypotheses import Hypothesis


class SituationOpener(Protocol):
    """``promote`` が台帳に要求する最小の口 (SituationStore が満たす)。

    全体を要求すると、テストが台帳の実装まで抱え込む。昇格が実際に使うのは
    「既にあるか」と「開く」の 2 つだけ。
    """

    def get_situation(self, situation_id: str) -> Any: ...

    def open_situation(self, **kwargs: Any) -> Any: ...


_log = get_logger(__name__)

#: config_store のキー (版履歴・revert の対象)。
QUESTIONS_KEY = "standing_questions"

#: 常設 situation の id 接頭辞 (現行 seed の `s-standing-` と同じ名前空間)。
_ID_PREFIX = "s-standing-q-"


def _question_id(draft: QuestionDraft) -> str:
    """問いの同一性 = 型 + スロット値。**決心や注記では変わらない**。

    同じ問いを二度昇格させても 1 件に収まる。決心を書き直しても id は不変
    (問いは不変で動くのは答えだけ、という §6d の不変条件と揃える)。
    """
    payload = json.dumps(
        {"frame_id": draft.frame_id, "slots": dict(sorted(draft.slots.items()))},
        ensure_ascii=False,
        sort_keys=True,
    )
    return _ID_PREFIX + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def list_questions(*, db_path: Path | None = None) -> list[dict[str, Any]]:
    """保存済みの昇格済み問い (順序は保存順)。"""
    from src.storage.config_store import get_config

    rows = get_config(QUESTIONS_KEY, db_path=db_path)
    return list(rows) if isinstance(rows, list) else []


def frame_id_for(situation_id: str, *, db_path: Path | None = None) -> str | None:
    """situation_id → 型。未登録 (現行 4 seed 等) は None。

    評価側はこれで仮説骨格を選ぶ。**引けないと趨勢の問いに事前配置の仮説で答える**
    ことになるので、呼出側は None のとき既定 (POSTURE) へ倒す。
    """
    return (
        next(
            (
                str(r.get("frame_id") or "")
                for r in list_questions(db_path=db_path)
                if r.get("situation_id") == situation_id
            ),
            None,
        )
        or None
    )


def hypotheses_for_standing(
    situation_id: str, *, db_path: Path | None = None
) -> tuple[Hypothesis, ...]:
    """常設 situation の ACH 仮説骨格を型から選ぶ。

    **fail-open** — 未登録 (現行 4 seed) も、config が読めないときも
    ``POSTURE_HYPOTHESES`` に倒す。ここで例外を出すと台帳の評価が止まるため、
    「型が引けない = 現行挙動」に落とす方が安全。
    """
    from src.assessment.question_frame import FRAME_BY_ID
    from src.synthesis.grounded.hypotheses import POSTURE_HYPOTHESES

    try:
        frame_id = frame_id_for(situation_id, db_path=db_path)
    except Exception as e:  # noqa: BLE001 — 保存層の障害で評価を止めない
        _log.warning("standing_frame_lookup_failed", situation_id=situation_id, error=str(e))
        return POSTURE_HYPOTHESES
    frame = FRAME_BY_ID.get(frame_id or "")
    return frame.hypotheses if frame else POSTURE_HYPOTHESES


#: 集約シグナル (構成比) を判定に供給すべき型。
#:
#: A (presence) は**要らない** — 2 本腕の証拠規則 (直接 R1/R3 + 間接 R2) で既に蓄積を
#: 扱っており、集約を足すと同じ観測を二重に数えることになる。
#: E (trend) / H (threshold) は変化・水準の判定なので、蓄積の側が無いと
#: 「新着数本を読んだ印象」で答えることになる (利用者指摘 2026-09-16)。
_FRAMES_NEEDING_AGGREGATE = frozenset({"trend", "threshold"})


def needs_aggregate_signal(situation_id: str, *, db_path: Path | None = None) -> bool:
    """この常設 situation の判定に集約シグナルを供給すべきか。"""
    try:
        frame_id = frame_id_for(situation_id, db_path=db_path)
    except Exception as e:  # noqa: BLE001 — 保存層の障害で評価を止めない
        _log.warning("standing_frame_lookup_failed", situation_id=situation_id, error=str(e))
        return False
    return (frame_id or "") in _FRAMES_NEEDING_AGGREGATE


def evidence_condition_for(situation_id: str, *, db_path: Path | None = None) -> dict[str, Any]:
    """問いの証拠条件 (集約シグナルは**同じ母集団**で測る — 別集計を作らない)。"""
    try:
        rows = list_questions(db_path=db_path)
    except Exception as e:  # noqa: BLE001
        _log.warning("standing_condition_lookup_failed", situation_id=situation_id, error=str(e))
        return {}
    for r in rows:
        if r.get("situation_id") == situation_id:
            cond = r.get("evidence_condition")
            return dict(cond) if isinstance(cond, dict) else {}
    return {}


def promote(
    draft: QuestionDraft,
    *,
    store: SituationOpener,
    now_iso: str,
    db_path: Path | None = None,
) -> str:
    """資格判定を通った起草を常設情報要求として開設する。返り値 = situation_id。

    Raises:
        ValueError: 資格要件を満たさないとき (落ちた理由をすべて含む)。
    """
    result = qualify(draft)
    if not result.ok:
        raise ValueError("資格要件を満たしていません: " + " / ".join(result.failures))

    sid = _question_id(draft)
    record = {
        "situation_id": sid,
        "frame_id": draft.frame_id,
        "slots": dict(draft.slots),
        "decision": draft.decision,
        "evidence_condition": draft.evidence_condition,
        "note": draft.note,
        "promoted_at": now_iso,
    }
    existing = list_questions(db_path=db_path)
    if any(r.get("situation_id") == sid for r in existing):
        return sid

    if store.get_situation(sid) is None:
        store.open_situation(
            situation_id=sid,
            title=question_text(draft),
            domain="cyber_incident",
            anchors=frozenset(),
            pir_ids=(),
            now_iso=now_iso,
            kind="standing",
        )

    from src.storage.config_store import save_config

    save_config(
        QUESTIONS_KEY,
        [*existing, record],
        note=f"問いの昇格: {question_text(draft)}",
        db_path=db_path,
    )
    _log.info("standing_question_promoted", situation_id=sid, frame=draft.frame_id)
    return sid
