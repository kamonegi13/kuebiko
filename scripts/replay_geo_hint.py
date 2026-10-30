"""戦域ヒント (INGEST_GEO_THEATRE_HINT) の再現 (GPU 不要・読み取り専用)。

``triage_rejections`` (--since 以降) の題に ``geo_theatre_reasons`` を当て、発火件数と戦域別の
内訳、裁定用の層化標本 (id と題のみ、本文は出さない) を出す。概要は保存されていないため
題だけの照合 (本番は題 + RSS 概要なので発火は下限寄り)。

使い方:
  set -a; . ./.env; set +a
  DATABASE_URL="postgresql://kuebiko:${POSTGRES_PASSWORD}@127.0.0.1:5433/kuebiko" \\
  uv run python scripts/replay_geo_hint.py [--since 2026-10-12] [--per-theatre 8] [--out s.jsonl]
DATABASE_URL 未設定なら SQLite fallback (data/run_history.db) を読む。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PGOPTIONS", "-c default_transaction_read_only=on")

import src.storage.db_backend as _db  # noqa: E402

_db.ensure_pg_schema = lambda *a, **k: None  # DDL を流さない

from src.cti.ingest_relevance import geo_theatre_reasons  # noqa: E402

DEFAULT_SINCE = "2026-10-12"
DEFAULT_PER_THEATRE = 8
SEED = 20261030


def load_rejections(since: str) -> list[dict[str, Any]]:
    """triage_rejections を SELECT のみで読む (id / title / feed / importance / reason)。"""
    conn = _db.connect()
    try:
        cur = conn.execute(
            "SELECT article_id, title, feed_title, importance, reason, ts "
            "FROM triage_rejections WHERE ts >= ? ORDER BY ts",
            [since],
        )
        return [
            {
                "id": r[0],
                "title": r[1],
                "feed": r[2],
                "importance": r[3],
                "reason": r[4],
                "ts": r[5],
            }
            for r in cur.fetchall()
        ]
    finally:
        conn.close()


def replay(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """戦域名 -> 発火した行 (複数戦域に当たる行は各戦域に入る)。"""
    by_theatre: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        for reason in geo_theatre_reasons(row["title"]):
            by_theatre[reason].append(row)
    return by_theatre


def stratified_sample(
    by_theatre: dict[str, list[dict[str, Any]]], per_theatre: int
) -> list[dict[str, Any]]:
    rng = random.Random(SEED)
    out: list[dict[str, Any]] = []
    for theatre in sorted(by_theatre):
        rows = by_theatre[theatre]
        picked = rng.sample(rows, min(per_theatre, len(rows)))
        out.extend(
            {"theatre": theatre, "id": r["id"], "title": r["title"], "reason": r["reason"][:80]}
            for r in picked
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default=DEFAULT_SINCE)
    ap.add_argument("--per-theatre", type=int, default=DEFAULT_PER_THEATRE)
    ap.add_argument("--out", type=Path, default=None, help="標本の jsonl 出力先")
    args = ap.parse_args()

    rows = load_rejections(args.since)
    by_theatre = replay(rows)
    fired_ids = {r["id"] for v in by_theatre.values() for r in v}
    print(f"rejections since {args.since}: {len(rows)}")
    print(f"fired (any theatre, title only): {len(fired_ids)}")
    for theatre in sorted(by_theatre):
        print(f"  {theatre}: {len(by_theatre[theatre])}")
    sample = stratified_sample(by_theatre, args.per_theatre)
    if args.out:
        args.out.write_text(
            "".join(json.dumps(s, ensure_ascii=False) + "\n" for s in sample), encoding="utf-8"
        )
        print(f"sample: {len(sample)} -> {args.out}")
    else:
        for s in sample:
            print(json.dumps(s, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
