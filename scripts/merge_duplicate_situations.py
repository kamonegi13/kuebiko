#!/usr/bin/env python3
"""重複して開設された情勢を統合する (2026-09-22)。

規則 (`match_claim`) は誤って繋ぐ一方で、**本当の重複は取りこぼしていた**。情勢どうしを
埋込で総当たりし、余弦が閾値以上の組を審判の判定で統合する。

⭐ **統合先は最初に開設された方** (証拠と版の履歴が長い側)。
⚠ 常設情報要求 (standing) は設計上 別物なので対象外 (中国 / ロシア / イラン / 北朝鮮の
  事前配置は余弦 0.86 前後で近いが、統合してはいけない)。

判定の入力は ``data/mlx/situation_dup_labels.json`` (Opus 盲検) と
``data/mlx/situation_dup_index.json``。既定は dry-run。

    docker exec kuebiko python scripts/merge_duplicate_situations.py [--apply]
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, "/app")

from src.assessment.situation_store import SituationStore  # noqa: E402

LABELS = Path("data/mlx/situation_dup_labels.json")
INDEX = Path("data/mlx/situation_dup_index.json")
#: 統合する確度。low は審判自身が迷っている組なので見送る (人の確認に回す)。
ACCEPT_CONFIDENCE = ("high", "medium")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default="data/run_history.db")
    args = ap.parse_args()

    labels = {int(k): v for k, v in json.loads(LABELS.read_text(encoding="utf-8")).items()}
    index = {r["id"]: r for r in json.loads(INDEX.read_text(encoding="utf-8"))}
    store = SituationStore(db_path=Path(args.db))
    rows = {r.situation_id: r for r in store.load_situations(("active", "dormant"))}

    now = datetime.now(UTC).isoformat()
    applied = skipped = 0
    for i, verdict in sorted(labels.items()):
        same, confidence, reason = verdict[0], verdict[1], verdict[2]
        pair = index.get(i)
        if not same or pair is None:
            continue
        if confidence not in ACCEPT_CONFIDENCE:
            print(f"  見送り (確度 {confidence}): {pair['a'][:36]} ← {pair['b'][:36]}", flush=True)
            skipped += 1
            continue
        a, b = rows.get(pair["a_id"]), rows.get(pair["b_id"])
        if a is None or b is None:
            print(f"  ⚠ 既に統合済か不在: {pair['a'][:36]}", flush=True)
            skipped += 1
            continue
        # ⭐ 統合先は先に開設された方 (履歴が長い)
        keep, dup = (a, b) if a.opened_at <= b.opened_at else (b, a)
        print(
            f"  {pair['cos']:.3f} ({confidence}) 統合先「{keep.title[:40]}」"
            f"\n            吸収「{dup.title[:40]}」 — {reason[:30]}",
            flush=True,
        )
        if not args.apply:
            continue
        moved = store.merge_situation(
            dup_id=dup.situation_id,
            into_id=keep.situation_id,
            basis=f"埋込 {pair['cos']:.3f} / 審判 {confidence}: {reason[:80]}",
            now_iso=now,
        )
        rows.pop(dup.situation_id, None)
        print(f"            → 証拠 {moved} 件を移送", flush=True)
        applied += 1

    mode = "適用" if args.apply else "dry-run"
    print(f"\n{mode}: {applied} 組 / 見送り {skipped} 組", flush=True)
    if not args.apply:
        print("書き込むには --apply を付ける", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
