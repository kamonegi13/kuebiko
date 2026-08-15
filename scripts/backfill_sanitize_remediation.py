"""LLM 自由記述列に残った HTML 残渣を除去する (2026-08-15)。

briefing.py の一括 sanitize 追加以前に永続化された行が対象。
本文の再生成はせず既存値をサニタイズするだけなので冪等。

    uv run python scripts/backfill_sanitize_remediation.py          # dry-run
    uv run python scripts/backfill_sanitize_remediation.py --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.storage.db_backend import connect, translate_sql  # noqa: E402
from src.tools.text_sanitizer import has_html_residue, sanitize_for_display  # noqa: E402

# articles の LLM 自由記述列 (briefing.LLM_FREE_TEXT_METADATA_KEYS と対応)
TARGET_COLUMNS: tuple[str, ...] = (
    "remediation",
    "technical_axis_summary",
    "socio_political_rationale",
    "summary",
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="実際に更新する")
    args = parser.parse_args()

    conn = connect()
    total_fixed = 0
    for column in TARGET_COLUMNS:
        rows = conn.execute(
            translate_sql(
                f"SELECT id, {column} FROM articles "  # noqa: S608 — column は定数 tuple 由来
                f"WHERE {column} LIKE '%</%' OR {column} LIKE '%<p>%' OR {column} LIKE '%<br%'"
            )
        ).fetchall()
        fixed = 0
        for row in rows:
            article_id, value = row[0], row[1]
            if not isinstance(value, str):
                continue
            cleaned = sanitize_for_display(value).strip()
            if cleaned == value:
                continue
            if args.apply:
                conn.execute(
                    translate_sql(f"UPDATE articles SET {column}=? WHERE id=?"),  # noqa: S608
                    (cleaned or None, article_id),
                )
            fixed += 1
        if args.apply:
            conn.commit()
        residue_left = sum(1 for r in rows if has_html_residue(str(r[1] or "")))
        print(f"{column}: 対象 {len(rows)} 件 / 修正 {fixed} 件 (残渣検出 {residue_left})")
        total_fixed += fixed
    print(f"{'適用' if args.apply else 'dry-run'}: 合計 {total_fixed} 件")


if __name__ == "__main__":
    main()
