"""既存の Grok 記事に per-tweet の source identity を遡及適用する。

2026-08-24 の本流化 (``grok_subarticle_source``) は **これから取り込む記事** にしか
効かない。既存の x.com 記事 913 件は feed_title='Grok' / feed_url='https://grok.com/'
のままで、**何人が報じても独立 1 媒体** として数えられ続ける。

やること:
1. x.com permalink を持つ記事の feed_title / feed_url を投稿者に書き換える
2. 影響を受けた事象アイテムの裏取り 3 値 (独立媒体数 / 国営 / 未分類) を再計算する
   — これらは群化時に確定した **保存列** なので、記事側を直しても自動では変わらない

実行例:
  docker compose exec kuebiko python scripts/eventnews_backfill.py --help   # 参考
  docker compose exec kuebiko python scripts/backfill_grok_source_identity.py --apply
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.eventnews.state import compute_source_breakdown
from src.storage.run_history import RunHistoryRepository

_X_PERMALINK = re.compile(r"^https?://(?:www\.)?x\.com/([A-Za-z0-9_]{1,15})/status/\d+")


def _rewrite_articles(repo: RunHistoryRepository, *, apply: bool) -> int:
    with repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            "SELECT article_id AS article_id, url AS url,"
            " COALESCE(feed_title,'') AS feed_title, COALESCE(feed_url,'') AS feed_url"
            " FROM articles WHERE POSITION(? IN url) > 0",
            ("//x.com/",),
        ).fetchall()
        updates: list[tuple[str, str, str]] = []
        for r in rows:
            m = _X_PERMALINK.match(str(r["url"]).strip())
            if not m:
                continue
            handle = m.group(1)
            feed_url, feed_title = f"https://x.com/{handle}", f"@{handle}"
            if str(r["feed_url"]) == feed_url and str(r["feed_title"]) == feed_title:
                continue
            updates.append((feed_title, feed_url, str(r["article_id"])))
        print(f"記事の書き換え対象: {len(updates)} / {len(rows)} 件")
        if apply and updates:
            for feed_title, feed_url, article_id in updates:
                conn.execute(
                    "UPDATE articles SET feed_title = ?, feed_url = ? WHERE article_id = ?",
                    (feed_title, feed_url, article_id),
                )
    return len(updates)


def _recompute_breakdowns(repo: RunHistoryRepository, *, apply: bool) -> int:
    """事象アイテムの裏取り 3 値を再計算する (保存列のため記事側の変更では動かない)。"""
    from datetime import UTC, datetime, timedelta

    from src.eventnews.models import ENTITY_FREQ_WINDOW_HOURS
    from src.ui.services.eventnews_hourly_job import _entity_counts, _load_members

    counts = _entity_counts(repo, datetime.now(UTC) - timedelta(hours=ENTITY_FREQ_WINDOW_HOURS))
    changed = 0
    for record in repo.list_event_items(origin="live", limit=40000):
        members_by_id = _load_members(repo, list(record.state.member_ids), counts)
        members = [members_by_id[a] for a in record.state.member_ids if a in members_by_id]
        if not members:
            continue
        b = compute_source_breakdown(members)
        if (
            b.independent == record.independent_sources
            and b.state_media == record.state_media_count
            and b.unclassified == record.unclassified_sources
            and b.best_tier == record.best_source_tier
        ):
            continue
        changed += 1
        if apply:
            repo.update_event_item(
                record.state.item_id,
                {
                    "independent_sources": b.independent,
                    "state_media_count": b.state_media,
                    "unclassified_sources": b.unclassified,
                    "best_source_tier": b.best_tier,
                },
            )
    print(f"裏取り 3 値の再計算対象: {changed} アイテム")
    return changed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="実際に書き換える (既定は dry-run)")
    args = ap.parse_args()

    repo = RunHistoryRepository()
    _rewrite_articles(repo, apply=args.apply)
    _recompute_breakdowns(repo, apply=args.apply)
    if not args.apply:
        print("(dry-run — --apply で書き換え)")


if __name__ == "__main__":
    main()
