#!/usr/bin/env python3
"""victim_country_gate (src/cti/victim_country_gate.py) を既存行へ遡及適用する。

監査 (data/eval/victim_country_audit_v1.jsonl) で判明した「非インシデント記事
(advisory/press/research/opinion/tutorial) の被害国誤付与」を、取込済みの
articles 行に対しても修正する。対象は:

  - victim_country_iso か victim_country_raw のどちらかが NULL でない
  - article_type が NON_INCIDENT_ARTICLE_TYPES に含まれる
  - その article_id に entity_type='victim_org' の article_entities 行が無い
  - (既定) created_at が直近 90 日以内 (``--days`` で変更)

articles は article_id が重複し得る (同じ記事を複数回処理した行が残る) ため、
対象 article_id に対応する行は **全て** null 化する。

既定は **dry-run** (集計のみ表示)。``--apply`` で初めて書き込む。``--apply`` は
書き込み前に対象行の (article_id, victim_country_iso, victim_country_raw,
victim_country_scope) を ``_backup_victim_country_gate_YYYYMMDD_HHMMSS`` に
コピーしてから 3 列を NULL 化する。

Usage (production = PG、コンテナ内で実行し DATABASE_URL を解決):
    docker exec kuebiko /app/.venv/bin/python3 \\
        /app/scripts/retro_victim_country_gate.py                    # dry-run
    docker exec kuebiko /app/.venv/bin/python3 \\
        /app/scripts/retro_victim_country_gate.py --days 180         # 窓を変更
    docker exec kuebiko /app/.venv/bin/python3 \\
        /app/scripts/retro_victim_country_gate.py --apply            # 実行
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.cti.victim_country_gate import NON_INCIDENT_ARTICLE_TYPES  # noqa: E402
from src.storage.db_backend import connect  # noqa: E402

#: created_at の既定窓 (日数)。
DEFAULT_WINDOW_DAYS = 90

_NON_INCIDENT_TYPES = sorted(NON_INCIDENT_ARTICLE_TYPES)


def _placeholders(n: int) -> str:
    return ",".join("?" for _ in range(n))


def _find_candidate_article_ids(cur: Any, since_iso: str) -> list[str]:
    """関門の条件に当たる article_id の一覧 (重複なし)。"""
    type_ph = _placeholders(len(_NON_INCIDENT_TYPES))
    cur.execute(
        f"""
        SELECT DISTINCT a.article_id
        FROM articles a
        WHERE (a.victim_country_iso IS NOT NULL OR a.victim_country_raw IS NOT NULL)
          AND a.article_type IN ({type_ph})
          AND a.created_at >= ?
          AND NOT EXISTS (
              SELECT 1 FROM article_entities e
              WHERE e.article_id = a.article_id AND e.entity_type = 'victim_org'
          )
        """,  # noqa: S608 — IN の件数分だけ placeholder、値はすべて bind
        (*_NON_INCIDENT_TYPES, since_iso),
    )
    return [str(r["article_id"]) for r in cur.fetchall()]


def _fetch_affected_rows(cur: Any, article_ids: list[str]) -> list[dict[str, Any]]:
    """対象 article_id に属する全行 (重複行も含む)。"""
    if not article_ids:
        return []
    id_ph = _placeholders(len(article_ids))
    cur.execute(
        f"""
        SELECT id, article_id, article_type, victim_country_iso,
               victim_country_raw, victim_country_scope
        FROM articles
        WHERE article_id IN ({id_ph})
        """,  # noqa: S608 — IN の件数分だけ placeholder、値はすべて bind
        tuple(article_ids),
    )
    return [dict(r) for r in cur.fetchall()]


def _backup_table_name() -> str:
    return "_backup_victim_country_gate_" + datetime.now(UTC).strftime("%Y%m%d_%H%M%S")


def main(*, apply: bool, days: int) -> int:
    since_iso = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    conn = connect()
    try:
        cur = conn.cursor()
        article_ids = _find_candidate_article_ids(cur, since_iso)
        rows = _fetch_affected_rows(cur, article_ids)

        print(
            f"=== victim_country_gate 遡及適用: 対象 article_id {len(article_ids)} 件 "
            f"/ 行 {len(rows)} 件 (直近 {days} 日) ==="
        )
        by_type = Counter(str(r["article_type"]) for r in rows)
        by_iso = Counter(str(r["victim_country_iso"]) for r in rows if r["victim_country_iso"])
        print("  article_type 別:")
        for t, n in by_type.most_common():
            print(f"    {t:<10} {n}")
        print("  victim_country_iso 別:")
        for iso, n in by_iso.most_common():
            print(f"    {iso:<4} {n}")

        if not apply:
            print("\n[dry-run] 書き込みなし。--apply で UPDATE を実行する。")
            return 0

        if not rows:
            print("\n[applied] 対象 0 件のため何もしない。")
            return 0

        backup_table = _backup_table_name()
        id_ph = _placeholders(len(article_ids))
        cur.execute(
            f"""
            CREATE TABLE {backup_table} AS
            SELECT id, article_id, victim_country_iso, victim_country_raw,
                   victim_country_scope
            FROM articles
            WHERE article_id IN ({id_ph})
            """,  # noqa: S608 — テーブル名は本スクリプトが生成した固定形式 (ユーザ入力なし)
            tuple(article_ids),
        )
        cur.execute(
            f"""
            UPDATE articles
            SET victim_country_iso = NULL,
                victim_country_raw = NULL,
                victim_country_scope = NULL
            WHERE article_id IN ({id_ph})
            """,  # noqa: S608
            tuple(article_ids),
        )
        conn.commit()
        print(f"\n[applied] {len(rows)} 行を null 化した (backup: {backup_table})。")
        return 0
    finally:
        conn.close()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="実際に UPDATE する (既定は dry-run)")
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_WINDOW_DAYS,
        help=f"created_at の窓 (既定 {DEFAULT_WINDOW_DAYS} 日)",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    sys.exit(main(apply=args.apply, days=args.days))
