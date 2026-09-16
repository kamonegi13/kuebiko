#!/usr/bin/env python3
"""常設情報要求 (PIR) の起草と昇格 — 2026-09-16。

設計: docs/pir_brief_design.md §6c/§6d/§6e。

**証拠条件は 2 本腕**にする (利用者指摘 2026-09-16「一記事で答えてもいいが、それだけでは
ダメ」)。既存の A 型 (事前配置) が R1-R3 でやっていた構造を踏襲する:

- **直接の腕**: 稀だが 1 本で決定的 (日本の被害そのもの)
- **間接の腕**: 個別には曖昧だが蓄積して意味を持つ (世界の重要インフラでの同種活動)

片方だけだと壊れる — 直接だけならフィードを読めば済み、間接だけだと決定的な 1 本が
構成比に希釈される。ACH が両者を重み付け、集約シグナル (段B-3d) が蓄積の側を供給する。

⚠ 「問いは日本・証拠は世界」は既存 R2 と同じ形で、意図的である。地域の姿勢は世界での
振る舞いから部分的に推論する (それが「他所で進行・当地に証拠なし」という競合仮説の根拠)。

使い方 (コンテナ内):
    docker exec kuebiko python scripts/promote_standing_questions.py            # dry-run
    docker exec kuebiko python scripts/promote_standing_questions.py --apply
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.assessment.question_draft import QuestionDraft, qualify, question_text  # noqa: E402
from src.assessment.question_frame import SCOPE_ALL_CI  # noqa: E402
from src.assessment.question_store import list_questions, promote  # noqa: E402
from src.assessment.situation_store import SituationStore  # noqa: E402
from src.cti.nisc_sectors import _CANONICAL_TO_NISC  # noqa: E402

#: 重要インフラとみなす canonical セクタ (NISC 分野の写像元)。SSoT 参照 — 複製しない。
_CI = sorted(_CANONICAL_TO_NISC)

_DECISION_PRIORITY = "防御の優先順位を変えるか (監視・パッチ・演習の重点配分)"
_DECISION_ALERT = "警報を出すか / 監視態勢を上げるか"


def _leaf(prop: str, op: str, value: Any) -> dict[str, Any]:
    return {"property": prop, "op": op, "value": value}


def _trend(threat: str) -> QuestionDraft:
    """型 E: 日本の重要インフラに対する {threat} は悪化しているか。

    直接 = 日本の被害 / 間接 = 世界の重要インフラでの同種活動。
    """
    return QuestionDraft(
        frame_id="trend",
        slots={"target_country": "JP", "target_scope": SCOPE_ALL_CI, "threat": threat},
        decision=_DECISION_PRIORITY,
        evidence_condition={
            "any": [
                {"all": [_leaf("intent", "eq", threat), _leaf("victim_country", "eq", "JP")]},
                {"all": [_leaf("intent", "eq", threat), _leaf("victim_sector", "in", _CI)]},
            ]
        },
        answer_can_move_ack=True,
        note="直接=日本被害 / 間接=世界のCI。集約シグナルが構成比の移動を供給する。",
    )


def _threshold(nation: str) -> QuestionDraft:
    """型 H: {nation} による日本の重要インフラへの活動は平時の水準を越えたか。

    直接 = 帰属済み × 日本被害 / 間接 = 帰属済み × CI 分野、および帰属済み × 質的に
    異なる行為 (事前配置・破壊)。H は「量」でなく「線を越えたか」なので、後者が効く。
    """
    return QuestionDraft(
        frame_id="threshold",
        slots={"subject": nation, "target_country": "JP", "target_scope": SCOPE_ALL_CI},
        decision=_DECISION_ALERT,
        evidence_condition={
            "any": [
                {
                    "all": [
                        _leaf("actor_nation", "in", [nation]),
                        _leaf("victim_country", "eq", "JP"),
                    ]
                },
                {
                    "all": [
                        _leaf("actor_nation", "in", [nation]),
                        _leaf("victim_sector", "in", _CI),
                    ]
                },
                {
                    "all": [
                        _leaf("actor_nation", "in", [nation]),
                        _leaf("intent", "in", ["prepositioning", "disruption"]),
                    ]
                },
            ]
        },
        answer_can_move_ack=True,
        note="直接=日本被害 / 間接=世界のCI + 質的に異なる行為。",
    )


#: 起草する問い。**コードではなくデータとして DB へ入る** (§6e) — この一覧は投入用の
#: 台本であって SSoT ではない。投入後の SSoT は config_store の standing_questions。
DRAFTS: tuple[QuestionDraft, ...] = (
    _trend("disruption"),
    _trend("espionage"),
    _threshold("cn"),
    _threshold("kp"),
    _threshold("ru"),
)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--apply", action="store_true", help="実際に昇格する (既定は dry-run)")
    args = ap.parse_args()

    existing = {r.get("situation_id") for r in list_questions()}
    print(f"既に昇格済み: {len(existing)} 件\n")

    failed = 0
    for d in DRAFTS:
        result = qualify(d)
        mark = "✓" if result.ok else "✗"
        print(f"{mark} [{d.frame_id}] {question_text(d)}")
        print(f"    決心: {d.decision}")
        if not result.ok:
            failed += 1
            for f in result.failures:
                print(f"    ✗ {f}")
    if failed:
        print(f"\n資格要件を満たさない問いが {failed} 件あります。中止します。")
        return 1

    if not args.apply:
        print("\n(dry-run — 変更なし。実行は --apply)")
        return 0

    store = SituationStore()
    now_iso = datetime.now(UTC).isoformat()
    for d in DRAFTS:
        sid = promote(d, store=store, now_iso=now_iso)
        state = "既存" if sid in existing else "新規"
        print(f"  {state} {sid}  {question_text(d)}")
    print(f"\n昇格完了。合計 {len(list_questions())} 件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
