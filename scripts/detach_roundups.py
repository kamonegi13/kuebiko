"""既存の事象からまとめ記事を外す (2026-10-02)。

まとめ記事 (週次まとめ・ダイジェスト・ニュースレター) は複数の出来事を並べる。事象の
構成記事にすると別の出来事が混ざり、まとめ記事を経由して無関係な記事が入り込む
(09-26 の検証で混入の主因)。2026-10-02 から群化はまとめ記事を参加させないが、
それ以前に入ったものが残っている。このスクリプトが一度だけ外す。

1. 生きている事象の構成記事のうち、種別が未分類のものを分類する (fast ティア)
2. 構成記事 2 件以上の事象にいるまとめ記事を、1 件ずつ単独の事象へ移す
   (related_to = 元の事象。読み手は関連事象欄から辿れる)
3. 構成が変わった事象の本文を作り直す (統合・分割と同じく、再生成までがセット)

使い方 (本番 DB、使い捨てコンテナで):
    docker compose run --rm --no-deps -T kuebiko python scripts/detach_roundups.py [--apply]
既定は dry-run。--no-generate で本文の再生成を外す。
"""

import argparse
import asyncio
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta

sys.path.insert(0, "/app")

from retro_split_events import _refresh_item_state, _split_target_id

from src.config_loader import load_app_config
from src.eventnews.models import ENTITY_FREQ_WINDOW_HOURS
from src.storage.run_history import RunHistoryRepository
from src.ui.services.eventnews_hourly_job import (
    _entity_counts,
    _load_members,
    _resolve_kinds,
    regenerate_pending_bodies,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--no-generate", action="store_true")
    ap.add_argument("--sleep", type=float, default=3.0)
    args = ap.parse_args()

    repo = RunHistoryRepository()
    records = [
        r
        for r in repo.list_event_items(origin="live", limit=20000)
        if not r.merged_into and len(r.state.member_ids) >= 2
    ]
    print(f"構成記事 2 件以上の事象: {len(records)}", flush=True)
    counts = _entity_counts(repo, datetime.now(UTC) - timedelta(hours=ENTITY_FREQ_WINDOW_HOURS))
    members = _load_members(repo, [a for r in records for a in r.state.member_ids], counts)

    missing = [m for m in members.values() if not m.kind]
    print(f"未分類の構成記事: {len(missing)} 件 → 分類する", flush=True)
    if missing:
        kinds = asyncio.run(_resolve_kinds(repo, load_app_config(), missing))
        members = {
            aid: replace(m, kind=kinds[aid]) if aid in kinds else m for aid, m in members.items()
        }

    changed = 0
    moved = 0
    for r in records:
        ids = [a for a in r.state.member_ids if a in members]
        roundups = [a for a in ids if members[a].is_roundup]
        rest = [a for a in ids if not members[a].is_roundup]
        if not roundups or not rest:
            continue  # まとめ記事が無い / まとめ記事だけの事象はそのまま
        changed += 1
        moved += len(roundups)
        titles = " / ".join(members[a].title[:40] for a in roundups)
        print(f"  {r.state.item_id} 残す {len(rest)} 件・外す {len(roundups)} 件: {titles}")
        if not args.apply:
            continue
        for aid in roundups:
            target = _split_target_id(repo, aid, r.state.item_id)
            if repo.get_event_item(target) is None:
                m = members[aid]
                repo.create_event_item(
                    item_id=target,
                    origin="live",
                    first_reported_at=m.anchor_ts,
                    last_reported_at=m.anchor_ts,
                    importance=m.importance,
                    related_to=r.state.item_id,
                )
            repo.move_event_member(
                article_id=aid,
                from_item=r.state.item_id,
                to_item=target,
                join_signal="roundup_detach",
            )
            _refresh_item_state(repo, target, [members[aid]])
        _refresh_item_state(repo, r.state.item_id, [members[a] for a in rest])

    mode = "適用" if args.apply else "dry-run"
    print(f"\n{mode}: {changed} 事象から まとめ記事 {moved} 件を外す", flush=True)
    if args.apply and changed and not args.no_generate:
        regenerate_pending_bodies(repo, args.sleep)
    if not args.apply:
        print("書き込むには --apply を付ける", flush=True)


if __name__ == "__main__":
    main()
