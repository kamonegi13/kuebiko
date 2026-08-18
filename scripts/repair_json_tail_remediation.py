#!/usr/bin/env python3
"""remediation に流れ込んだ「JSON の続き」を切り落とす一回限りの修復。

2026-08-18 の調査で、LLM が JSON 文字列を閉じ損ねると後続フィールドの JSON が
そのまま値に入ることが分かった (全期間 33 件、2026-06-09〜08-18)。300 字 truncate の
おかげで UI 上は「少し長い対処」に見え、2 か月検知できなかった。

再発は ``src/pipeline/briefing.py`` の cut_json_tail で塞いだ。本 script は
**既存データの掃除**だけを行う。切るのは署名以降のみで、先頭の正しい 1 文は残す。

⚠ 飲み込まれた analyst_note は復旧できない (DB に列が無く、投稿時点で失われている)。

Usage::

    uv run python scripts/repair_json_tail_remediation.py          # 対象一覧 (dry-run)
    uv run python scripts/repair_json_tail_remediation.py --apply  # 実際に更新
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO if (_REPO / "src").is_dir() else Path("/app")))

from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.tools.text_sanitizer import cut_json_tail, has_json_tail  # noqa: E402

SELECT = """
    SELECT id, remediation FROM articles
    WHERE remediation IS NOT NULL AND remediation <> ''
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="実際に UPDATE する")
    args = ap.parse_args()

    repo = RunHistoryRepository()
    with repo._connect() as conn:  # noqa: SLF001 — 一回限りの保守
        rows = conn.execute(SELECT).fetchall()
        targets = []
        for row in rows:
            values = list(dict(row).values()) if not isinstance(row, (list, tuple)) else list(row)
            article_id, remediation = values[0], str(values[1] or "")
            if not has_json_tail(remediation):
                continue
            cleaned = cut_json_tail(remediation).strip()
            targets.append((article_id, remediation, cleaned))

        print(f"対象 {len(targets)} 件")
        empties = [t for t in targets if not t[2]]
        print(f"  うち切ると空になる (先頭から壊れている): {len(empties)} 件")
        for article_id, before, after in targets[:5]:
            print(f"  [{article_id}] {len(before)}字 → {len(after)}字")
            print(f"      after: {after[:90]}")
        if not args.apply:
            print("\n--apply を付けると更新します (dry-run)")
            return 0

        updated = 0
        for article_id, _before, after in targets:
            # 切ると空になるものは NULL にする (壊れた断片を残さない)
            conn.execute(
                "UPDATE articles SET remediation = ? WHERE id = ?",
                (after or None, article_id),
            )
            updated += 1
        conn.commit()
        print(f"\n更新 {updated} 件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
