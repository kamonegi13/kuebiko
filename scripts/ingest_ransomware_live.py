#!/usr/bin/env python3
"""ransomware.live 構造化被害者を articles に取り込む (被害状況コレクタ)。

JP 被害者 → japan_watch 投稿 + status='posted' / 非 JP → status='collected' (地図のみ)。
通常は APScheduler が定期実行するが、初回 backfill や手動実行に使う。

Usage (production = PG、コンテナ内で実行):
    # dry-run (件数集計のみ、投稿も DB 書込もしない)
    docker exec kuebiko /app/.venv/bin/python3 \
        scripts/ingest_ransomware_live.py --dry-run
    # 実行 (global recent + JP 履歴 backfill)
    docker exec kuebiko /app/.venv/bin/python3 \
        scripts/ingest_ransomware_live.py --apply --backfill-jp
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.sources.ransomware_ingest import run_ingest  # noqa: E402


async def _main(apply: bool, backfill_jp: bool) -> int:
    stats = await run_ingest(
        backfill_country="JP" if backfill_jp else None,
        dry_run=not apply,
    )
    mode = "APPLY" if apply else "DRY-RUN"
    print(
        f"[{mode}] ransomware.live 取込: fetched={stats['fetched']} new={stats['new']} "
        f"(posted_jp={stats['posted_jp']} / collected={stats['collected']} / "
        f"dup={stats['duplicate']}) reconciled(既存→重複)={stats['reconciled']} "
        f"news_tagged(自動辞書)={stats['news_tagged']} skipped(既存)={stats['skipped']}"
    )
    if not apply:
        print("--apply で実際に投稿(JP)・DB 取込する。")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    sys.exit(
        asyncio.run(_main(apply="--apply" in args, backfill_jp="--backfill-jp" in args))
    )
