"""actor_provisional の地政学ノイズ過去蓄積の掃除 (監査 2026-08-01 ⑦)。

2026-08-01 の取込 filter (is_geopolitical_noise) 導入以前に蓄積した国名・政府・軍系の
provisional 行 (iran 41 / russia 31 / china 22 等、上位 20 の過半) を除去する。
これらは STRONG_ANCHOR (状況割当) と検索 pivot を汚しており、放置すると誤連結の温床。

処理:
1. article_entities (entity_type='actor_provisional') の value を filter に通し対象確定
2. 退避テーブルへ CREATE TABLE AS SELECT (可逆)
3. 対象行 DELETE
4. actor_update_proposals の該当 pending (corpus:<key>) を rejected 化 — dedup_key で
   再提案されない却下学習をそのまま使う (掃除後の再発防止)

既定 dry-run。--apply で実行:
  docker exec kuebiko python -m scripts.purge_geopolitical_provisional [--apply]
"""

from __future__ import annotations

import argparse

from src.cti.actor_candidates import is_geopolitical_noise
from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)

_BACKUP = "_backup_provisional_purge_20260801"


def _run(apply: bool, repo: RunHistoryRepository | None = None) -> None:
    repo = repo if repo is not None else RunHistoryRepository()
    mode = "APPLY" if apply else "DRY-RUN"
    with repo._connect() as con:  # noqa: SLF001 — 修復スクリプト
        rows = con.execute(
            "SELECT value, COUNT(DISTINCT article_id) AS n FROM article_entities"
            " WHERE entity_type='actor_provisional' GROUP BY value"
        ).fetchall()
        targets = sorted(
            (str(r["value"]), int(r["n"])) for r in rows if is_geopolitical_noise(str(r["value"]))
        )
        print(f"\n=== actor_provisional 地政学ノイズ掃除 ({mode}) ===")
        print(f"distinct 値 {len(rows)} 中、対象 {len(targets)} 値:")
        for v, n in sorted(targets, key=lambda t: -t[1]):
            print(f"  {v:30} {n} 記事")
        if not targets:
            print("(対象なし)")
            return
        if apply:
            values = [v for v, _ in targets]
            ph = ",".join("?" for _ in values)
            con.execute(f"DROP TABLE IF EXISTS {_BACKUP}")  # noqa: S608 — 固定名
            con.execute(
                f"CREATE TABLE {_BACKUP} AS SELECT * FROM article_entities"  # noqa: S608
                f" WHERE entity_type='actor_provisional' AND value IN ({ph})",
                values,
            )
            deleted = con.execute(
                "DELETE FROM article_entities"  # noqa: S608 — ph は ? 固定
                f" WHERE entity_type='actor_provisional' AND value IN ({ph})",
                values,
            ).rowcount
            rejected = con.execute(
                "UPDATE actor_update_proposals SET status='rejected'"
                f" WHERE status='pending' AND dedup_key IN ({ph})",
                [f"corpus:{v}" for v in values],
            ).rowcount
            print(f"\nDELETE {deleted} 行 (退避 = {_BACKUP}) / pending 提案 rejected {rejected} 件")
        else:
            print("\n(dry-run — --apply で実行)")
    _log.info("purge_geopolitical_provisional_done", apply=apply, target_values=len(targets))


def main() -> None:
    ap = argparse.ArgumentParser(description="actor_provisional の地政学ノイズ掃除")
    ap.add_argument("--apply", action="store_true", help="実際に削除する (既定は dry-run)")
    _run(ap.parse_args().apply)


if __name__ == "__main__":
    main()
