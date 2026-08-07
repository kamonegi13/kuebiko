"""pattern_5 退役 (2026-08-01) の移行 backfill — UAT/APT designation の暗定 entity 種まき。

リコール層の収穫は取込時のみ働くため、正規表現に UAT/APT を追加しただけでは
既存コーパスの designation が暗定 entity にならず、裏取り集計 (3 記事) に乗るまで
時間がかかる。直近 90 日の本文を一回だけ掃査して種をまく (INSERT OR IGNORE で冪等)。

実行 (host):
    DATABASE_URL=postgresql://... PYTHONPATH=. uv run python \
        scripts/backfill_uat_apt_provisional.py [--apply]
"""

from __future__ import annotations

import argparse
import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.cti.actor_candidates import harvest_candidates
from src.cti.actor_normalizer import load_actor_aliases
from src.storage.run_history import RunHistoryRepository

_WINDOW_DAYS = 90
# 引き継ぎ分の prefix のみ種まき (storm/unc 等は稼働中の収穫が既に担っている)
_NEW_PREFIXES = ("uat-", "apt")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="entity を実際に挿入する")
    args = parser.parse_args()

    repo = RunHistoryRepository(db_path=Path("data/run_history.db"))
    registry = load_actor_aliases()
    since = datetime.now(UTC) - timedelta(days=_WINDOW_DAYS)

    from src.storage.db_backend import connect as backend_connect

    con = backend_connect(Path("data/run_history.db"))
    if hasattr(con, "row_factory"):
        con.row_factory = sqlite3.Row
    try:
        # LIKE prefilter で本文取得を絞る (APT は一般語だが regex 側で番号必須)
        rows = con.execute(
            """
            SELECT article_id, MAX(body) AS body FROM articles
             WHERE datetime(created_at) >= datetime(?)
               AND body IS NOT NULL AND body <> ''
               AND (body LIKE ? OR body LIKE ?)
             GROUP BY article_id
            """,
            (since.isoformat(), "%UAT-%", "%APT%"),
        ).fetchall()
    finally:
        con.close()

    per_key: Counter[str] = Counter()
    added_total = 0
    for r in rows:
        body = str(r["body"] or "")
        cands = harvest_candidates(body=body, primary_actor_id="", registry=registry)
        keys = [
            c.key
            for c in cands
            if c.signal == "vendor_designation" and c.key.startswith(_NEW_PREFIXES)
        ]
        if not keys:
            continue
        for k in keys:
            per_key[k] += 1
        if args.apply:
            added_total += repo.add_article_entities(
                str(r["article_id"]), [("actor_provisional", k) for k in keys]
            )

    print(f"scanned={len(rows)} articles (window {_WINDOW_DAYS}d, prefilter UAT-/APT)")
    for key, n in per_key.most_common(30):
        print(f"  {key}: {n} 記事")
    if args.apply:
        print(f"inserted={added_total} 暗定 entity (冪等)")
    else:
        print("dry-run — --apply で挿入")


if __name__ == "__main__":
    main()
