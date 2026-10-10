"""台帳 ACH の当日対象選定ヘルパ。

夜間 deep-review 本体 (run_deep_review) は 2026-09-10 に廃止 (flip 定着 48% = コイン投げ)。
`scripts/build_sft_teacher_ach.py` が教師データ組み立てで `_select_targets` / `_MAX_SOURCES`
を使うため、選定ロジックのみ残している。
"""

from __future__ import annotations

from src.assessment.situation_store import RevisionRow, SituationStore
from src.assessment.standing import STANDING_KIND

# 1 Situation あたり ACH に渡す当日証拠の上限 (昼の増分と同じ思想の有界化)
_MAX_SOURCES = 8


def _select_targets(
    store: SituationStore,
    revs_by_sid: dict[str, list[RevisionRow]],
    *,
    since_iso: str,
    cap: int,
) -> tuple[list[tuple[str, list[str]]], int]:
    """当日 revision + 当日証拠のある active Situation を standing 優先で選ぶ。

    返り値 = ([(sid, 当日証拠 aids)], 証拠なしで除外した数)。証拠の有無を cap 消費前に
    フィルタする — sweep 等の判断のみの revision (再接地の材料なし) に枠を使わない
    (実機スモーク 2026-07-24: standing が判断のみ revision で cap を占有し実質 0 件になった)。
    """
    scored: list[tuple[int, int, str, list[str]]] = []
    skipped = 0
    for sid, revs in revs_by_sid.items():
        row = store.get_situation(sid)
        if row is None or row.status != "active":
            continue
        today_aids = store.evidence_ids_added_since(sid, since_iso=since_iso)
        if not today_aids:
            skipped += 1
            continue
        is_standing = 1 if row.kind == STANDING_KIND else 0
        scored.append((is_standing, len(revs), sid, today_aids))
    scored.sort(key=lambda t: (-t[0], -t[1], t[2]))
    return [(sid, aids) for _, _, sid, aids in scored[:cap]], skipped
