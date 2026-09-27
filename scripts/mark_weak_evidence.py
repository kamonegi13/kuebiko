#!/usr/bin/env python3
"""台帳の弱い証拠に印を刻む (2026-09-27、設計は src/assessment/evidence_weak.py)。

関門の判定 (ML) は ``ASSIGN_ML=1`` の環境で動くため、**コンテナ内**で流す::

    docker exec -w /app kuebiko python scripts/mark_weak_evidence.py          # 件数だけ (書かない)
    docker exec -w /app kuebiko python scripts/mark_weak_evidence.py --apply  # 刻む

戻すとき: ``UPDATE situation_evidence SET weak_at = NULL WHERE weak_at = '<刻んだ時刻>'``
(刻んだ時刻は --apply の出力に出る)。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.assessment.evidence_weak import scan_weak_evidence  # noqa: E402
from src.assessment.situation_store import SituationStore  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402


async def _main(apply: bool) -> int:
    repo = RunHistoryRepository()
    store = SituationStore()
    scan = await scan_weak_evidence(repo, store)
    per_sit = Counter(sid for sid, _ in scan.weak)
    print(f"判定={scan.judge} 採点 {scan.checked} / 弱い {len(scan.weak)} / 台帳 {len(per_sit)}")
    for sid, n in per_sit.most_common(10):
        print(f"  {sid} 弱い {n}")
    if not apply:
        print("(書き込みなし。刻むときは --apply)")
        return 0
    stamp = datetime.now(UTC).isoformat()
    n = store.mark_weak(list(scan.weak), weak_at=stamp)
    print(f"刻んだ {n} 件 weak_at={stamp}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="印を刻む (既定は件数だけ)")
    return asyncio.run(_main(ap.parse_args().apply))


if __name__ == "__main__":
    raise SystemExit(main())
