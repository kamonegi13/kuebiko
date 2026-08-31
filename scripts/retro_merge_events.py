"""既に割れている事象を、いまの群化規則で統合し直す (遡及)。

毎時の群化は **新しい記事を既存の事象へ入れる**だけで、**既にできた事象どうし**を
突き合わせない。そのため取り込み順や当時の閾値の都合で割れたものが、そのまま
残り続ける (実測 2026-08-31: 読者に重複が見えている組が 19、ATF は 4 事象に分裂)。

統合先は **最初に立った事象** — URL がそこに残る。吸収した側は ``merged_into`` を
立てて全経路から外れる (一覧・詳細・公開面・写しはすべて既に対応済み)。

使い方:
    python scripts/retro_merge_events.py --since 2026-08-17 [--apply]
既定は dry-run。``--apply`` を付けたときだけ書き込む。
"""

import argparse
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta

sys.path.insert(0, "/app")

import numpy as np

from src.eventnews.grouping import build_join_entities, edge_is_allowed
from src.eventnews.models import ENTITY_FREQ_WINDOW_HOURS, JOIN_ENTITY_TYPES, WINDOW_HOURS
from src.eventnews.runner import _max_importance
from src.eventnews.state import compute_source_breakdown
from src.storage.run_history import RunHistoryRepository
from src.ui.services.eventnews_hourly_job import _entity_counts, _load_members, _load_vectors


def _find(parent: dict[str, str], x: str) -> str:
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-08-17")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    repo = RunHistoryRepository()
    records = [
        r
        for r in repo.list_event_items(origin="live", limit=20000)
        if not r.merged_into and str(r.state.first_reported_at) >= args.since
    ]
    single = [r for r in records if len(r.state.member_ids) == 1]
    print(f"対象 (単独メンバーの事象): {len(single)} 件", flush=True)

    art_of = {r.state.member_ids[0]: r for r in single}
    counts = _entity_counts(repo, datetime.now(UTC) - timedelta(hours=ENTITY_FREQ_WINDOW_HOURS))
    members = _load_members(repo, list(art_of), counts)

    raw: list[tuple[str, str, str]] = []
    for aid, m in members.items():
        for et, val in m.entities:
            if et in JOIN_ENTITY_TYPES:
                raw.append((aid, et, val))
    # 埋込は毎時ジョブと同じ loader から取る (取得経路を分けると挙動が一致しない)
    vecs: dict[str, np.ndarray] = {}
    for aid, v in _load_vectors(repo, list(members)).items():
        n = float(np.linalg.norm(v))
        if n:
            vecs[aid] = v / n
    # counts のキーは _entity_counts が既に join_entity_key で作っている
    # (もう一度通すと victim_org が二重正規化されて lookup が外れる)
    ents = build_join_entities(raw, counts)

    by_ent: dict[tuple[str, str], list[str]] = defaultdict(list)
    for aid, es in ents.items():
        for e in es:
            by_ent[e].append(aid)

    pairs: set[tuple[str, str]] = set()
    for aids in by_ent.values():
        for i in range(len(aids)):
            for j in range(i + 1, len(aids)):
                a, b = sorted((aids[i], aids[j]))
                if a in vecs and b in vecs:
                    pairs.add((a, b))

    edges: list[tuple[str, str, float]] = []
    for a, b in pairs:
        ra, rb = art_of[a], art_of[b]
        if ra.state.item_id == rb.state.item_id:
            continue
        gap = abs((ra.state.last_reported_at - rb.state.last_reported_at).total_seconds())
        if gap > WINDOW_HOURS * 3600:
            continue
        # ⚠ 条件を自前で書かない。参加判定と**同じ関数**を通す
        #    (2026-08-31 に自前の条件を持っていてガードが片方だけ効かなかった)。
        shared = tuple(sorted(ents[a] & ents[b]))
        cos = float(np.dot(vecs[a], vecs[b]))
        if edge_is_allowed(ents[a], ents[b], shared, cos):
            edges.append((ra.state.item_id, rb.state.item_id, cos))

    parent = {r.state.item_id: r.state.item_id for r in single}
    for x, y, _ in edges:
        rx, ry = _find(parent, x), _find(parent, y)
        if rx != ry:
            parent[rx] = ry
    groups: dict[str, list[str]] = defaultdict(list)
    for item_id in parent:
        groups[_find(parent, item_id)].append(item_id)
    merges = {k: v for k, v in groups.items() if len(v) > 1}
    print(f"辺 {len(edges)} 本 → 統合する群 {len(merges)} 個", flush=True)

    rec_of = {r.state.item_id: r for r in single}
    applied = 0
    for ids in merges.values():
        # ⭐ 統合先は **最初に立った事象** — URL がそこに残る
        ordered = sorted(ids, key=lambda i: rec_of[i].state.first_reported_at)
        target, absorbed = ordered[0], ordered[1:]
        all_articles = [a for i in ordered for a in rec_of[i].state.member_ids]
        titles = " / ".join(members[rec_of[i].state.member_ids[0]].title[:30] for i in ordered)
        print(f"  {target} ← {len(absorbed)} 件 : {titles}", flush=True)
        if not args.apply:
            continue
        for aid in all_articles:
            repo.add_event_member(
                item_id=target,
                article_id=aid,
                joined_at=datetime.now(UTC),
                contributed_new_facts=0,
                join_signal="retro_merge",
            )
        mm = _load_members(repo, all_articles, counts)
        breakdown = compute_source_breakdown([mm[a] for a in all_articles if a in mm])
        importance = ""
        for i in ordered:
            importance = _max_importance(importance, rec_of[i].state.importance)
        repo.update_event_item(
            target,
            {
                # 版を作り直させる (メンバーが変わったので本文が古い)
                "current_version": 0,
                "importance": importance,
                "best_source_tier": breakdown.best_tier,
                "independent_sources": breakdown.independent,
                "state_media_count": breakdown.state_media,
                "unclassified_sources": breakdown.unclassified,
                "last_reported_at": max(rec_of[i].state.last_reported_at for i in ordered),
                "updated_at": datetime.now(UTC),
            },
        )
        for i in absorbed:
            repo.update_event_item(i, {"merged_into": target, "updated_at": datetime.now(UTC)})
        applied += 1

    print(f"\n{'適用' if args.apply else 'dry-run'}: {applied}/{len(merges)} 群", flush=True)
    if not args.apply:
        print("書き込むには --apply を付ける", flush=True)


main()
