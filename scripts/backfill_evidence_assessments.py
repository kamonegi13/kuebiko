#!/usr/bin/env python3
"""証拠台帳の状態分離 (2026-07-16) に伴う一回性 backfill。

背景: 旧 add_evidence は既存 (situation_id, article_id) ペアを冪等 skip したため、
毎時割当 (bare 行: polarity='neutral'/excerpt='') が先行した記事の ACH 評価結果が
永久に落ちていた (実測: 台帳 neutral 909 行中 878 行が excerpt 空)。schema 側の
宣言的 backfill (excerpt あり → assessed_at=added_at) は適用済み前提で、本スクリプトは:

  1. **失われた評価の復元**: status_synthesis.tradecraft->grounded_estimate に埋め込まれた
     過去の judgment.evidence (excerpt あり) を、judgment.id = situation_id で突合して
     situation_evidence に upsert する。generated_at 昇順で処理し最新判定が勝つ。
     既に新しい評価がある行 (assessed_at >= 当該 record) は触らない。
  2. **旧キューとの整合**: read_at IS NULL の bare 行のうち、旧キュー定義
     (added_at > 最終 revision) で既に脱落していた行へ read_at=added_at を刻む
     (歴史を「読んだことにする」のではなく「旧仕様で排出済み」の宣言 — UI では
     assessed_at が無い限り「未評価」のまま正直に表示される)。現役キュー相当の行
     (最終 revision より新しい) は read_at NULL のまま残し、新キューが引き継ぐ。

冪等: 1 は assessed_at ガード付き upsert、2 は read_at IS NULL 条件付き UPDATE。
LLM 呼出なし・決定論のみ。

Usage (production PG、コンテナ内):
    docker cp scripts/backfill_evidence_assessments.py kuebiko:/tmp/bea.py
    docker exec kuebiko python /tmp/bea.py --dry-run
    docker exec kuebiko python /tmp/bea.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO if (_REPO / "src").is_dir() else Path("/app")))

from src.storage.db_backend import connect  # noqa: E402

_DB_PATH = Path("data/run_history.db")  # DATABASE_URL 設定時は PG に接続される


def _open(db_path: Path = _DB_PATH) -> Any:
    """列名 access 可能な connection を返す (PG wrapper は既定で可、SQLite は Row 設定)。"""
    import sqlite3

    conn = connect(db_path)
    if isinstance(conn, sqlite3.Connection):
        conn.row_factory = sqlite3.Row
    return conn


def _stats(conn: Any) -> dict[str, int]:
    row = conn.execute(
        "SELECT COUNT(*) AS total,"
        " SUM(CASE WHEN assessed_at IS NOT NULL THEN 1 ELSE 0 END) AS assessed,"
        " SUM(CASE WHEN read_at IS NULL THEN 1 ELSE 0 END) AS unread"
        " FROM situation_evidence"
    ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "assessed": int(row["assessed"] or 0),
        "unread": int(row["unread"] or 0),
    }


def restore_assessments(conn: Any, *, dry_run: bool) -> tuple[int, int]:
    """過去 grounded_estimate から評価を復元する。返り値=(更新, 挿入)。"""
    sids = {
        str(r["situation_id"])
        for r in conn.execute("SELECT situation_id FROM situations").fetchall()
    }
    records = conn.execute(
        "SELECT generated_at, tradecraft FROM status_synthesis"
        " WHERE tradecraft IS NOT NULL ORDER BY generated_at ASC"
    ).fetchall()
    updated = inserted = 0
    for rec in records:
        gen_at = str(rec["generated_at"])
        raw = rec["tradecraft"]
        if isinstance(raw, dict):  # PG は JSONB 列 → psycopg が dict で返す
            tc = raw
        else:
            try:
                tc = json.loads(raw or "{}")
            except (TypeError, ValueError):
                continue
        est = tc.get("grounded_estimate") or {}
        for j in est.get("judgments", []):
            sid = str(j.get("id", ""))
            if sid not in sids:
                continue  # 旧 j1..jN 形式 (台帳以前) は突合不能なので skip
            for e in j.get("evidence", []):
                excerpt = str(e.get("excerpt", "")).strip()
                aid = str(e.get("article_id", ""))
                if not excerpt or not aid:
                    continue  # 再水和で混入した bare 行は評価ではない
                row = conn.execute(
                    "SELECT assessed_at FROM situation_evidence"
                    " WHERE situation_id=? AND article_id=?",
                    (sid, aid),
                ).fetchone()
                vals = (
                    str(e.get("polarity", "neutral")),
                    str(e.get("attribution_basis", "unattributed")),
                    excerpt[:500],
                    str(e.get("source_tier", "unknown")),
                    gen_at,
                )
                if row is None:
                    inserted += 1
                    if not dry_run:
                        conn.execute(
                            "INSERT OR IGNORE INTO situation_evidence"
                            " (situation_id, article_id, polarity, attribution_basis,"
                            " excerpt, source_tier, added_at, assigned_by, read_at,"
                            " assessed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (sid, aid, *vals[:4], gen_at, "seed", gen_at, gen_at),
                        )
                elif row["assessed_at"] is None or str(row["assessed_at"]) < gen_at:
                    updated += 1
                    if not dry_run:
                        conn.execute(
                            "UPDATE situation_evidence SET polarity=?,"
                            " attribution_basis=?, excerpt=?, source_tier=?,"
                            " assessed_at=?, read_at=COALESCE(read_at, ?)"
                            " WHERE situation_id=? AND article_id=?",
                            (*vals, gen_at, sid, aid),
                        )
    return updated, inserted


def drain_legacy_queue(conn: Any, *, dry_run: bool) -> int:
    """旧キュー定義で既に脱落していた bare 行に read_at=added_at を宣言する。"""
    where = (
        " WHERE read_at IS NULL AND added_at <= COALESCE("
        "(SELECT MAX(r.created_at) FROM situation_revisions r"
        " WHERE r.situation_id = situation_evidence.situation_id), '')"
    )
    if dry_run:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM situation_evidence" + where  # noqa: S608
        ).fetchone()
        return int(row["n"] or 0)
    cur = conn.execute(
        "UPDATE situation_evidence SET read_at = added_at" + where  # noqa: S608
    )
    return int(getattr(cur, "rowcount", 0) or 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="書き込みせず件数のみ表示")
    args = parser.parse_args()

    conn = _open()
    try:
        before = _stats(conn)
        print(f"before: {before}")
        updated, inserted = restore_assessments(conn, dry_run=args.dry_run)
        drained = drain_legacy_queue(conn, dry_run=args.dry_run)
        if not args.dry_run:
            conn.commit()
        after = _stats(conn)
        mode = "DRY-RUN " if args.dry_run else ""
        print(f"{mode}restored assessments: updated={updated} inserted={inserted}")
        print(f"{mode}legacy queue drained (read_at 付与): {drained}")
        print(f"after: {after}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
