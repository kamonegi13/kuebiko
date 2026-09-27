"""敵の作戦ではない作戦名を campaign 指標から外す (2026-09-27)。

取込の除外 (``mention_tagger.non_adversary_operations``、config/cti/non_adversary_operations.yaml)
と **同じ一覧** で過去分を掃除する。軍事・政策・法執行の作戦名は STIX の Campaign ではなく、
台帳の割当の強い鍵として無関係な記事を吸い寄せていた。

削除する行は時刻つきの退避表へ複製してから消す。既定 dry-run。--apply で実行:
  docker exec -w /app kuebiko python -m scripts.purge_non_adversary_campaigns [--apply]
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime

from src.cti.mention_tagger import non_adversary_operations
from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)

_PREFIX = "operation "


def is_non_adversary(value: str) -> bool:
    """campaign の値 ("Operation <Name>") が敵の作戦ではない作戦名か。"""
    v = value.strip().lower()
    name = v[len(_PREFIX) :] if v.startswith(_PREFIX) else v
    return name in non_adversary_operations()


def _run(apply: bool, repo: RunHistoryRepository | None = None) -> None:
    repo = repo if repo is not None else RunHistoryRepository()
    mode = "APPLY" if apply else "DRY-RUN"
    with repo._connect() as con:  # noqa: SLF001 — 修復スクリプト
        rows = [
            dict(r)
            for r in con.execute(
                "SELECT id AS eid, value FROM article_entities WHERE entity_type='campaign'"
            ).fetchall()
        ]
        targets = [r for r in rows if is_non_adversary(str(r["value"]))]
        print(f"\n=== 敵の作戦ではない campaign の掃除 ({mode}) ===")
        print(f"走査 {len(rows)} 行 / 対象 {len(targets)} 行")
        for v, n in Counter(str(r["value"]) for r in targets).most_common(20):
            print(f"  {n:4d} {v}")
        if not targets or not apply:
            print("\n(dry-run — --apply で実行)" if targets else "(対象なし)")
            return
        backup = "_backup_non_adversary_campaign_" + datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        ids = [int(r["eid"]) for r in targets]
        ph = ",".join("?" for _ in ids)
        con.execute(
            f"CREATE TABLE {backup} AS SELECT * FROM article_entities WHERE id IN ({ph})",  # noqa: S608
            ids,
        )
        deleted = con.execute(
            f"DELETE FROM article_entities WHERE id IN ({ph})",  # noqa: S608 — ph は ? 固定
            ids,
        ).rowcount
        print(f"\nDELETE {deleted} 行 (退避 = {backup})")
    _log.info("purge_non_adversary_campaigns_done", apply=apply, targets=len(targets))


def main() -> None:
    ap = argparse.ArgumentParser(description="敵の作戦ではない campaign の掃除")
    ap.add_argument("--apply", action="store_true", help="実際に削除する (既定は dry-run)")
    _run(ap.parse_args().apply)


if __name__ == "__main__":
    main()
