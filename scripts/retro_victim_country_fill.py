#!/usr/bin/env python3
"""victim_country_fill (src/cti/victim_country_fill.py) を既存行へ遡及適用する。

対象: 速報 (breaking) のサイバー記事で、被害国 (iso / raw / scope) がすべて空の行。
規則で国を足せる行だけを更新し、victim_country_raw に出所 (``inferred:<規則>``) を残す
(= この接頭辞で後から全件を特定して戻せる)。LLM が値を出していた行には触れない。

既定は **dry-run** (規則別の件数と標本を表示)。``--apply`` で初めて書き込み、書き込み前に
対象行の (id, article_id, victim_country_iso, victim_country_raw, victim_country_scope) を
``_backup_victim_country_fill_YYYYMMDD_HHMMSS`` へ退避する。

Usage (production = PG、コンテナ内で実行し DATABASE_URL を解決):
    docker exec kuebiko /app/.venv/bin/python3 \\
        /app/scripts/retro_victim_country_fill.py                 # dry-run
    docker exec kuebiko /app/.venv/bin/python3 \\
        /app/scripts/retro_victim_country_fill.py --days 180      # 窓を変更
    docker exec kuebiko /app/.venv/bin/python3 \\
        /app/scripts/retro_victim_country_fill.py --apply         # 実行

戻し方 (apply 後): 退避表から復元する、または
    UPDATE articles SET victim_country_iso=NULL, victim_country_raw=NULL
     WHERE victim_country_raw LIKE 'inferred:%';
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.cti.victim_country_fill import (  # noqa: E402
    FILL_ARTICLE_TYPE,
    FILL_CATEGORIES,
    INFERRED_RAW_PREFIX,
    CountryFill,
    infer_victim_country,
    load_org_history,
)
from src.storage.db_backend import connect  # noqa: E402

#: created_at の既定窓 (日数)。
DEFAULT_WINDOW_DAYS = 90
#: dry-run で表示する標本の件数。
SAMPLE_SIZE = 15

_CATEGORIES = sorted(FILL_CATEGORIES)


def _placeholders(n: int) -> str:
    return ",".join("?" for _ in range(n))


def _fetch_empty_rows(cur: Any, since_iso: str) -> list[dict[str, Any]]:
    cat_ph = _placeholders(len(_CATEGORIES))
    cur.execute(
        f"""
        SELECT id, article_id, category, title, summary
        FROM articles
        WHERE status = 'posted' AND article_type = ?
          AND category IN ({cat_ph})
          AND COALESCE(victim_country_iso, '') = ''
          AND COALESCE(victim_country_raw, '') = ''
          AND victim_country_scope IS NULL
          AND created_at >= ?
        """,  # noqa: S608 — IN の件数分だけ placeholder、値はすべて bind
        (FILL_ARTICLE_TYPE, *_CATEGORIES, since_iso),
    )
    return [dict(r) for r in cur.fetchall()]


def _fetch_victim_orgs(cur: Any, article_ids: list[str]) -> dict[str, list[str]]:
    orgs: dict[str, list[str]] = defaultdict(list)
    if not article_ids:
        return orgs
    cur.execute(
        f"""
        SELECT article_id, value FROM article_entities
        WHERE entity_type = 'victim_org' AND article_id IN ({_placeholders(len(article_ids))})
        """,  # noqa: S608 — IN の件数分だけ placeholder、値はすべて bind
        tuple(article_ids),
    )
    for r in cur.fetchall():
        orgs[str(r["article_id"])].append(str(r["value"]))
    return orgs


def _plan(cur: Any, since_iso: str) -> list[tuple[dict[str, Any], CountryFill]]:
    rows = _fetch_empty_rows(cur, since_iso)
    orgs = _fetch_victim_orgs(cur, sorted({str(r["article_id"]) for r in rows}))
    history = load_org_history()
    plan: list[tuple[dict[str, Any], CountryFill]] = []
    for row in rows:
        fill = infer_victim_country(
            article_type=FILL_ARTICLE_TYPE,
            category=str(row["category"] or ""),
            victim_orgs=orgs.get(str(row["article_id"]), []),
            title=str(row["title"] or ""),
            summary=str(row["summary"] or ""),
            org_history=history,
        )
        if fill is not None:
            plan.append((row, fill))
    return plan


def _backup_table_name() -> str:
    return "_backup_victim_country_fill_" + datetime.now(UTC).strftime("%Y%m%d_%H%M%S")


def _apply(cur: Any, plan: list[tuple[dict[str, Any], CountryFill]]) -> str:
    ids = [int(row["id"]) for row, _ in plan]
    backup = _backup_table_name()
    cur.execute(
        f"""
        CREATE TABLE {backup} AS
        SELECT id, article_id, victim_country_iso, victim_country_raw, victim_country_scope
        FROM articles WHERE id IN ({_placeholders(len(ids))})
        """,  # noqa: S608 — テーブル名は本スクリプトが生成した固定形式、id は bind
        tuple(ids),
    )
    for row, fill in plan:
        cur.execute(
            "UPDATE articles SET victim_country_iso = ?, victim_country_raw = ? WHERE id = ?",
            (fill.iso, f"{INFERRED_RAW_PREFIX}{fill.rule}", row["id"]),
        )
    return backup


def main(*, apply: bool, days: int) -> int:
    since_iso = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    conn = connect()
    try:
        cur = conn.cursor()
        plan = _plan(cur, since_iso)
        print(f"=== victim_country_fill 遡及適用: 補完できる行 {len(plan)} 件 (直近 {days} 日) ===")
        print("  規則別:", dict(Counter(f.rule for _, f in plan)))
        print("  国別:", dict(Counter(f.iso for _, f in plan)))
        for row, fill in plan[:SAMPLE_SIZE]:
            print(f"    id={row['id']} {fill.iso} ({fill.rule}) {str(row['title'])[:60]}")
        if not apply:
            print("\n[dry-run] 書き込みなし。--apply で UPDATE を実行する。")
            return 0
        if not plan:
            print("\n[applied] 対象 0 件のため何もしない。")
            return 0
        backup = _apply(cur, plan)
        conn.commit()
        print(f"\n[applied] {len(plan)} 行を更新した (backup: {backup})。")
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
