"""既存の台帳に型 (actor / campaign) を付ける (2026-09-27、判定は situation_track.py)。

開設時と同じ判定を、開設記事 (証拠の assigned_by='seed') で行う。型が未設定の event の台帳だけ。
既定 dry-run。--apply で書く:
  docker exec -w /app kuebiko python -m scripts.backfill_situation_track [--apply]
"""

from __future__ import annotations

import argparse
from collections import Counter

from src.assessment.situation_store import SituationStore
from src.assessment.situation_track import decide_track_for, registry_is_group


def main() -> None:
    ap = argparse.ArgumentParser(description="既存の台帳に型を付ける")
    ap.add_argument("--apply", action="store_true", help="書き込む (既定は件数だけ)")
    apply = ap.parse_args().apply
    store = SituationStore()
    rows = [
        r
        for r in store.load_situations(("active", "dormant", "closed"))
        if r.kind == "event" and r.track is None
    ]
    seeds = store.evidence_ids_by_situation([r.situation_id for r in rows], assigned_by="seed")
    aids = sorted({a for v in seeds.values() for a in v})
    subjects = store.subjects_for_articles(aids)
    is_group = registry_is_group()
    counts: Counter[tuple[str, str]] = Counter()
    for r in rows:
        track = decide_track_for(
            r.anchors, sorted(seeds.get(r.situation_id, set())), subjects, is_group=is_group
        )
        counts[(r.status, track)] += 1
        if apply:
            store.set_track(r.situation_id, track)
    print(f"対象 {len(rows)} 件 ({'書き込み' if apply else 'dry-run'})")
    for (status, track), n in sorted(counts.items()):
        print(f"  {status:8} {track:9} {n}")


if __name__ == "__main__":
    main()
