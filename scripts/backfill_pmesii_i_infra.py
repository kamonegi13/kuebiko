#!/usr/bin/env python3
"""PMESII I-infra 軸の暗黒期間 backfill (2026-07-16 監査対処、決定論のみ)。

背景: pmesii_axes は summarizer 末尾フィールドとして 2026-06-01 頃から T/I-infra が
枯死した (I-infra は feed_default の残滓 6 件/30日のみ)。取込側は focused 分類器 +
NISC 写像の決定論フロアに移設済み。本スクリプトは暗黒期間の記事へ **決定論フロアのみ**
を適用する (LLM ゼロ・即時)。nisc_sector_for は canonical セクター + タイトル/要約の
キーワード層を持つ SSoT (src/cti/nisc_sectors.py) — 取込側フロアと同一判定。

冪等: pmesii_i_infra=0 の行のみ 1 に更新する (既存 1 は触らない)。
LLM による補完 backfill は、本 backfill 後の回復率をベースライン (292 件/週) と
突き合わせてから要否を判断する (docs/surfaces_remediation_design_2026_07_16.md §1)。

Usage (production PG、コンテナ内):
    docker cp scripts/backfill_pmesii_i_infra.py kuebiko:/tmp/bpi.py
    docker exec kuebiko python /tmp/bpi.py --dry-run
    docker exec kuebiko python /tmp/bpi.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO if (_REPO / "src").is_dir() else Path("/app")))

from src.cti.nisc_sectors import nisc_sector_for  # noqa: E402
from src.storage.db_backend import connect  # noqa: E402

_DB_PATH = Path("data/run_history.db")  # DATABASE_URL 設定時は PG に接続される
_DARK_START = "2026-06-01"  # T/I-infra 崩落の起点 (週次実測: 5/25 低下 → 6/1 崩落)
_BATCH = 500


def _open(db_path: Path = _DB_PATH) -> Any:
    import sqlite3

    conn = connect(db_path)
    if isinstance(conn, sqlite3.Connection):
        conn.row_factory = sqlite3.Row
    return conn


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="書き込みせず件数のみ表示")
    parser.add_argument("--since", default=_DARK_START, help="対象期間の開始日 (既定=暗黒起点)")
    args = parser.parse_args()

    conn = _open()
    try:
        rows = conn.execute(
            "SELECT id, article_id, title, summary, victim_sector_canonical"
            " FROM articles"
            " WHERE created_at >= ? AND status IN ('posted', 'collected')"
            " AND (pmesii_i_infra IS NULL OR pmesii_i_infra = 0)",
            (args.since,),
        ).fetchall()
        print(f"candidates: {len(rows)} (since {args.since})")
        hit_ids: list[int] = []
        for r in rows:
            sector = nisc_sector_for(
                r["victim_sector_canonical"],
                str(r["title"] or ""),
                str(r["summary"] or ""),
            )
            if sector is not None:
                hit_ids.append(int(r["id"]))
        print(f"deterministic I-infra hits: {len(hit_ids)}")
        if args.dry_run:
            print("DRY-RUN: no writes")
            return 0
        for i in range(0, len(hit_ids), _BATCH):
            chunk = hit_ids[i : i + _BATCH]
            ph = ",".join("?" for _ in chunk)
            conn.execute(
                f"UPDATE articles SET pmesii_i_infra = 1 WHERE id IN ({ph})",  # noqa: S608
                chunk,
            )
        conn.commit()
        print(f"updated: {len(hit_ids)}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
