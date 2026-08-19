"""出典ドメインが IOC として保存された行の掃除 (2026-08-19)。

取込 filter (``src.cti.ioc_source_filter``) と **同じ SSoT** を使い、過去分を掃除する。
判定基準は「出典系の URL は IOC ではない。含めないこと」と明記していたが、実測で
ioc_domain/ioc_url 3,403 件中 44 件が購読ソースのドメイン、32 件がその記事自身の
出典ホストだった。

⚠ dry-run で値を必ず目視すること — 「購読しているベンダのドメインが本物の IOC」
(そのサイトが改ざんされ配布元になった等) は理論上ありうる。

既定 dry-run。--apply で実行:
  docker exec kuebiko python -m scripts.purge_source_reference_iocs [--apply]
"""

from __future__ import annotations

import argparse

from src.cti.ioc_source_filter import is_source_reference, source_hosts
from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)

_BACKUP = "_backup_ioc_source_ref_20260819"


def _run(apply: bool, repo: RunHistoryRepository | None = None) -> None:
    repo = repo if repo is not None else RunHistoryRepository()
    mode = "APPLY" if apply else "DRY-RUN"
    hosts = source_hosts()
    with repo._connect() as con:  # noqa: SLF001 — 修復スクリプト
        rows = con.execute(
            "SELECT ae.id AS eid, ae.entity_type, ae.value, a.url, a.title"
            " FROM article_entities ae JOIN articles a ON a.article_id = ae.article_id"
            " WHERE ae.entity_type IN ('ioc_domain','ioc_url')"
        ).fetchall()
        targets = [
            r
            for r in rows
            if is_source_reference(str(r["value"]), article_url=str(r["url"] or ""), hosts=hosts)
        ]
        print(f"\n=== 出典ドメインの IOC 掃除 ({mode}) — 購読ソース {len(hosts)} host ===")
        print(f"対象 {len(targets)} 行 / 走査 {len(rows)} 行:")
        for r in targets:
            print(f"  {r['entity_type']:11} {str(r['value'])[:48]:50} | {str(r['title'])[:50]}")
        if not targets:
            print("(対象なし)")
            return
        if apply:
            ids = [int(r["eid"]) for r in targets]
            ph = ",".join("?" for _ in ids)
            con.execute(f"DROP TABLE IF EXISTS {_BACKUP}")  # noqa: S608 — 固定名
            con.execute(
                f"CREATE TABLE {_BACKUP} AS SELECT * FROM article_entities"  # noqa: S608
                f" WHERE id IN ({ph})",
                ids,
            )
            deleted = con.execute(
                f"DELETE FROM article_entities WHERE id IN ({ph})",  # noqa: S608 — ph は ? 固定
                ids,
            ).rowcount
            print(f"\nDELETE {deleted} 行 (退避 = {_BACKUP})")
        else:
            print("\n(dry-run — --apply で実行。値を目視し本物の IOC が無いか確認)")
    _log.info("purge_source_reference_iocs_done", apply=apply, targets=len(targets))


def main() -> None:
    ap = argparse.ArgumentParser(description="出典ドメインの IOC 掃除")
    ap.add_argument("--apply", action="store_true", help="実際に削除する (既定は dry-run)")
    _run(ap.parse_args().apply)


if __name__ == "__main__":
    main()
