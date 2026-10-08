"""領域をまたぐ線 (サイバー × 地政学) の試作検証: 型ごとの本数・対照との比・盲検用サンプルを出す。

使い方: PYTHONPATH=. python scripts/graphrag_crossdomain_probe.py [--days 90] [--sample 15]
本番は変更しない (読み取りのみ)。
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from src.graph.crossdomain import KINDS, count_by_kind, derive_crossdomain, permuted_baseline
from src.graph.crossdomain_load import load_cross_events
from src.storage.run_history import RunHistoryRepository


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--sample", type=int, default=15)
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--out", default="data/eval/crossdomain_probe.json")
    args = ap.parse_args()

    repo = RunHistoryRepository()
    events = load_cross_events(repo, days=args.days)
    by_id = {e.item_id: e for e in events}
    lines = derive_crossdomain(events)
    real = count_by_kind(lines)
    base = permuted_baseline(events, rounds=args.rounds)
    print(
        f"geo={sum(e.domain == 'geo' for e in events)} cyber={sum(e.domain == 'cyber' for e in events)}"
    )
    rng = random.Random(7)
    samples = []
    for kind in KINDS:
        lift = real[kind] / base[kind] if base[kind] else float("inf")
        print(f"{kind:9s} 実測={real[kind]:6d} 対照={base[kind]:9.1f} lift={lift:5.2f}")
        pool = [x for x in lines if x.kind == kind]
        for x in rng.sample(pool, min(args.sample, len(pool))):
            g, c = by_id[x.geo], by_id[x.cyber]
            samples.append(
                {
                    "kind": kind,
                    "nations": x.nations,
                    "gap_days": x.gap_days,
                    "geo_id": g.item_id,
                    "geo_headline": g.headline,
                    "cyber_id": c.item_id,
                    "cyber_headline": c.headline,
                    "cyber_intents": sorted(c.intents),
                }
            )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {"real": real, "baseline": base, "samples": samples}, ensure_ascii=False, indent=1
        )
    )
    print("->", out)


if __name__ == "__main__":
    main()
