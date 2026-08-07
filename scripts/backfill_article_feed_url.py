#!/usr/bin/env python3
"""Source Identity Decoupling Stage 2: 過去記事に安定 source キー feed_url を後付けする。

Stage 1 で新記事は feed_url を持つが、既存記事は NULL。本 script は現レジストリの
``{feed_title -> url}`` 逆引きで既存記事に feed_url を充足する。

設計:
- 対象は ``feed_url IS NULL`` の行のみ → 冪等 (再実行で漏れだけ埋まる)。
- レジストリ (list_sources) の各 source について ``feed_title`` 一致行へ ``url`` を流す。
- feed_title がドリフト/削除済で一致しない記事は NULL のまま (Stage 3 のクエリが
  feed_title に fallback するため失わない)。
- 取り込み並行に安全: NULL 行のみ UPDATE するので新記事 (既に feed_url 充足) と衝突しない。

Usage:
    docker exec kuebiko /app/.venv/bin/python3 -m scripts.backfill_article_feed_url
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.logging_config import get_logger  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.ui.api._source_manager import list_sources  # noqa: E402

_log = get_logger(__name__)


def main() -> int:
    repo = RunHistoryRepository()

    before_missing, total = repo.count_articles_missing_feed_url()
    _log.info("feed_url_backfill_start", missing=before_missing, total=total)

    # レジストリ: feed_title -> url (= 安定 source キー)。title 衝突時は最後を採用。
    title_to_url: dict[str, str] = {}
    for s in list_sources():
        if s.title and s.url:
            title_to_url[s.title] = s.url

    updated = 0
    for title, url in title_to_url.items():
        n = repo.backfill_feed_url_by_title(feed_title=title, feed_url=url)
        if n:
            updated += n
            _log.info("feed_url_backfilled", feed_title=title[:60], url=url[:60], rows=n)

    after_missing, _ = repo.count_articles_missing_feed_url()
    coverage = (total - after_missing) / total * 100 if total else 0.0
    _log.info(
        "feed_url_backfill_complete",
        updated=updated,
        still_missing=after_missing,
        coverage_pct=round(coverage, 1),
    )
    print(
        f"updated={updated} still_missing={after_missing}/{total} "
        f"coverage={coverage:.1f}% (NULL は Stage3 クエリが feed_title fallback)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
