#!/usr/bin/env python3
"""枠 A ② の入力: s23i の期間の F3 pair_shadow 組 (118 組) を JSONL にする (GPU-TOOLS-PAIR)。

定義 (台帳 PHASE0_BATCHES_v6 §RP-2): F3 (data/eval/f3_gold_v2.jsonl) の source=pair_shadow を
``event_pair_shadow`` の left_id/right_id (両向き) に結び、llm_same が付いていて observed_at が
s23i の期間 (既定 2026-10-10T00:34Z〜2026-10-26T11:22Z) の組だけを残す。
出力は **id のみ** (記事の本文・要約・理由は持たない)。DB は SELECT のみ (読み取り専用接続)。

使い捨てのコンテナで (本番の DATABASE_URL を読み取り専用で使う。ホストの uv は SQLite に落ちる):
    docker compose run --rm --no-deps -T kuebiko \
        python scripts/gpu_window/build_pair_input.py --out data/eval/pair_control/pairs_s23i.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

F3_PATH = Path("data/eval/f3_gold_v2.jsonl")
OUT = Path("data/eval/pair_control/pairs_s23i.jsonl")
S23I_START = "2026-10-10T00:34:00+00:00"
S23I_END = "2026-10-26T11:22:59+00:00"
EXPECTED_PAIRS = 118


def load_f3_pairs(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """source=pair_shadow の組を id のみの形にする (理由・ラベルの文は持ち込まない)。"""
    out: list[dict[str, Any]] = []
    for r in rows:
        if r.get("source") != "pair_shadow":
            continue
        out.append(
            {
                "left_id": str(r["a"]),
                "right_id": str(r["b"]),
                "f3_id": str(r["id"]),
                "stratum": r.get("stratum"),
                "weight": r.get("weight"),
            }
        )
    return out


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def has_s23i_verdict(conn: Any, left_id: str, right_id: str, start: str, end: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM event_pair_shadow"
        " WHERE ((left_id=? AND right_id=?) OR (left_id=? AND right_id=?))"
        " AND llm_same IS NOT NULL AND observed_at >= ? AND observed_at <= ? LIMIT 1",
        (left_id, right_id, right_id, left_id, start, end),
    ).fetchone()
    return row is not None


def select_pairs(
    candidates: Sequence[dict[str, Any]], conn: Any, start: str, end: str
) -> list[dict[str, Any]]:
    return [
        p for p in candidates if has_s23i_verdict(conn, p["left_id"], p["right_id"], start, end)
    ]


def write_jsonl(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--f3", default=str(F3_PATH))
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--start", default=S23I_START)
    ap.add_argument("--end", default=S23I_END)
    ap.add_argument("--dry-run", action="store_true", help="F3 の組数だけ数える (DB に触れない)")
    args = ap.parse_args()
    candidates = load_f3_pairs(read_jsonl(Path(args.f3)))
    if args.dry_run:
        print(f"[pair-input] dry-run: F3 pair_shadow {len(candidates)} 組 -> {args.out}")
        return 0
    from readonly_conn import open_read_only

    from src.storage.db_backend import connect
    from src.storage.run_history import DEFAULT_DB_PATH

    conn = open_read_only(connect, DEFAULT_DB_PATH)
    try:
        picked = select_pairs(candidates, conn, args.start, args.end)
    finally:
        conn.close()
    write_jsonl(Path(args.out), picked)
    note = "" if len(picked) == EXPECTED_PAIRS else f" (台帳の期待 {EXPECTED_PAIRS} と違う)"
    print(f"[pair-input] {len(picked)} 組 -> {args.out}{note}")
    return 0 if len(picked) == EXPECTED_PAIRS else 1


if __name__ == "__main__":
    raise SystemExit(main())
