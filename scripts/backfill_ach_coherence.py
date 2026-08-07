"""canonical (最新 revision) の ACH 二重表現不整合を是正する one-shot backfill。

2026-07-25 ACH 整合監査: 旧 apply_adversarial がスカラ leading だけ flip しマトリクスを
更新しなかったため、111 situation 中 9 件の最新 revision が自己矛盾 (例: ANCHOR-CI 事案 =
マトリクス反証済みの reporting_artifact が公式見立て)。コード側は是正済みのため、
本スクリプトで既存 canonical だけを是正する。

- 履歴 revision は改変しない (監査記録として保全)。
- 是正は rev+1 の訂正 revision として追記 (delta_note に理由明示 = 透明・可逆)。
- 判定規律は src/assessment/coherence.py (pure) — dry-run で計画を確認してから --apply。

usage (コンテナ内実行 — DATABASE_URL が compose 内でのみ解決するため):
  docker exec kuebiko python -m scripts.backfill_ach_coherence            # dry-run
  docker exec kuebiko python -m scripts.backfill_ach_coherence --apply
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import UTC, datetime

from src.assessment.coherence import plan_coherence_fix
from src.assessment.situation_store import SituationStore

_RUN_ID = "ach-coherence-backfill"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="訂正 revision を書き込む")
    args = parser.parse_args()

    store = SituationStore()
    scanned = fixed = 0
    for row in store.load_situations(statuses=("active", "dormant", "closed")):
        latest = store.latest_revision(row.situation_id)
        if latest is None:
            continue
        scanned += 1
        try:
            hypotheses = json.loads(latest.hypotheses_json or "[]")
        except (json.JSONDecodeError, ValueError):
            print(f"  SKIP {row.situation_id} rev{latest.rev}: hypotheses JSON 破損")
            continue
        if not isinstance(hypotheses, list):
            continue
        plan = plan_coherence_fix(latest.leading_hypothesis, hypotheses)
        if plan.action == "none":
            continue
        fixed += 1
        print(
            f"  {plan.action.upper():12s} {row.situation_id} rev{latest.rev}: "
            f"{latest.leading_hypothesis} -> {plan.new_leading} | {plan.note}"
        )
        if not args.apply:
            continue
        corrective = replace(
            latest,
            rev=0,  # add_revision が採番
            run_id=_RUN_ID,
            leading_hypothesis=plan.new_leading,
            hypotheses_json=json.dumps(list(plan.hypotheses), ensure_ascii=False),
            delta_type="hypothesis_flip" if plan.action == "revert_flip" else "no_change",
            delta_note=plan.note,
            created_at=datetime.now(UTC).isoformat(),
        )
        written = store.add_revision(corrective)
        print(f"    -> rev{written.rev} 追記")

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] scanned={scanned} 是正対象={fixed}")


if __name__ == "__main__":
    main()
