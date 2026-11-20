#!/usr/bin/env python3
"""名前解決の一時失敗で unsafe_url (恒久扱い) になった記事を再取得のキューへ戻す (2026-11-20)。

11-19 監査 D2 P0-3b。名前解決の失敗が SSRF の拒否と同じ unsafe_url に潰れ、ホスト側の
通信断に当たった記事の本文が永久に feed 要約のままだった。**いま** その host が public な
アドレスに解決できる行だけを ``extraction_failure_reason=NULL``・``refetch_attempts=0`` に
戻す (通常の再取得の対象。dns_error にしないのは、見送り扱いの理由は作成から 3 日を
過ぎると再取得の対象外になるため)。private 等へ解決する host は SSRF の遮断として残す。

使い方 (本番の DB を読む。コンテナの中で):
    docker compose run --rm --no-deps -T kuebiko python scripts/requeue_dns_unsafe_url.py
    ... --apply   # 退避表 _backup_unsafe_url_requeue_<日付> を作ってから更新する
既定は dry-run (host ごとの件数だけ出す)。ログに本文・URL のパスは出さない。
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.tools.url_guard import UnsafeUrlError, assert_safe_public_url  # noqa: E402

_LOOKBACK_DAYS = 90
_SELECT = (
    "SELECT DISTINCT article_id, url FROM articles"
    " WHERE extraction_failure_reason = 'unsafe_url'"
    " AND (body IS NULL OR body_source = 'feed_summary')"
    " AND url IS NOT NULL AND url <> ''"
    " AND datetime(created_at) >= datetime('now', ?)"
)


def _resolvable_now(host: str) -> bool:
    try:
        assert_safe_public_url(f"https://{host}/")
    except UnsafeUrlError:
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="退避してから更新する (既定 dry-run)")
    args = ap.parse_args()
    repo = RunHistoryRepository()
    with repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute(_SELECT, (f"-{_LOOKBACK_DAYS} days",)).fetchall()
    by_host: dict[str, list[str]] = {}
    for r in rows:
        host = (urlparse(str(r["url"])).hostname or "").lower()
        by_host.setdefault(host, []).append(str(r["article_id"]))
    ok_hosts = {h for h in by_host if h and _resolvable_now(h)}
    counts = Counter({h: len(ids) for h, ids in by_host.items()})
    print(f"unsafe_url の行: {len(rows)} 記事 / host {len(by_host)} 件")
    for host, n in counts.most_common():
        print(f"  {'戻す' if host in ok_hosts else '残す'} {n:4d}  {host}")
    targets = [aid for h in ok_hosts for aid in by_host[h]]
    print(f"戻す対象: {len(targets)} 記事")
    if not args.apply or not targets:
        return 0
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    backup = f"_backup_unsafe_url_requeue_{stamp}"
    marks = ",".join("?" for _ in targets)
    with repo.atomic(), repo._connect() as conn:  # noqa: SLF001
        conn.execute(
            f"CREATE TABLE {backup} AS SELECT id, article_id, extraction_failure_reason,"  # noqa: S608
            f" refetch_attempts FROM articles WHERE article_id IN ({marks})"
            " AND extraction_failure_reason = 'unsafe_url'",
            tuple(targets),
        )
        cur = conn.execute(
            "UPDATE articles SET extraction_failure_reason = NULL, refetch_attempts = 0"  # noqa: S608
            f" WHERE article_id IN ({marks}) AND extraction_failure_reason = 'unsafe_url'",
            tuple(targets),
        )
    print(f"更新: {cur.rowcount} 行 (退避表 {backup})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
