#!/usr/bin/env python3
"""要約埋込を過去記事ぶん作り置きする (2026-09-21、一度きり)。

なぜ要るか: 事象どうしの統合を毎時回すには、全事象のメンバー記事の要約埋込が
要る。以前は**その場で作って捨てて**いたため、全期間で総当たりすると 13,000 件を
毎回作り直すことになり、ML は一瞬なのに埋込生成で数十分かかっていた。

⚠ 本文の埋込 (article_embeddings) には**触らない**。あれは意味的重複排除でも
使っており、入れ替えると別の機能に影響する。

⚠ GPU を使う。本番の重いジョブ (朝夕ブリーフ・spotlight・週次) と重ねない。
中断しても保存済みは残るので、再実行すれば続きから進む。

    docker exec kuebiko python scripts/backfill_summary_embeddings.py --limit 20000
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.config_loader import load_app_config  # noqa: E402
from src.eventnews.models import ENTITY_FREQ_WINDOW_HOURS  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.ui.services.eventnews_hourly_job import (  # noqa: E402
    _embed_summaries,
    _entity_counts,
    _load_members,
)

_BATCH = 200


async def main_async(args: argparse.Namespace) -> int:
    repo = RunHistoryRepository()
    cfg = load_app_config()
    records = [r for r in repo.list_event_items(origin="live", limit=40000) if not r.merged_into]
    ids = list(dict.fromkeys(a for r in records for a in r.state.member_ids))[: args.limit]
    have = set(repo.load_summary_embeddings(ids))
    todo = [a for a in ids if a not in have]
    print(
        f"事象 {len(records)} / メンバー記事 {len(ids)} / 保存済み {len(have)} / 未作成 {len(todo)}"
    )
    if not todo:
        print("作るものがありません")
        return 0

    counts = _entity_counts(repo, datetime.now(UTC) - timedelta(hours=ENTITY_FREQ_WINDOW_HOURS))
    t0 = time.monotonic()
    done = 0
    for i in range(0, len(todo), _BATCH):
        chunk = todo[i : i + _BATCH]
        members = _load_members(repo, chunk, counts)
        # _embed_summaries が保存済みを再利用し、足りない分だけ作って保存する
        got = await _embed_summaries(cfg, list(members.values()), repo=repo)
        done += len(got)
        el = time.monotonic() - t0
        rate = done / el if el else 0
        left = (len(todo) - (i + len(chunk))) / rate if rate else 0
        print(
            f"  {i + len(chunk):5d}/{len(todo)}  ({rate:.1f} 件/秒, 残り {left / 60:.0f} 分)",
            flush=True,
        )
    print(f"完了: {done} 件 / {time.monotonic() - t0:.0f} 秒")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=40000)
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
