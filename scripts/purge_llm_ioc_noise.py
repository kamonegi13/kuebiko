"""LLM 経由で保存された IOC の誤りの掃除 (2026-09-27)。

取込の関門 (``ioc_extractor.filter_llm_iocs`` / ``persistence._classify_ioc_type``) と **同じ SSoT**
で過去分を掃除する。実測で ioc_domain 3,631 件中 361 件がファイル名 (``setup.mjs`` 等)、
ioc_ip に公開 DNS (8.8.8.8 / 1.1.1.1) があった。

⚠ 保存行には「本文経路か LLM 経路か」の由来が無い。本文経路は攻撃の文脈があれば公開 DNS を
残すので、公開 DNS の行は dry-run で記事の見出しを目視すること。

既定 dry-run。--apply で実行 (対象行を退避表へ複製してから削除):
  docker exec kuebiko python -m scripts.purge_llm_ioc_noise [--apply]
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping
from typing import Any

from src.cti.ioc_extractor import _BENIGN_PUBLIC_DNS, _is_benign_ipv4, looks_like_filename
from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)

_BACKUP = "_backup_llm_ioc_noise_20260927"


def is_noise(entity_type: str, value: str) -> bool:
    """取込の関門と同じ基準で、保存済みの IOC が誤りか。"""
    v = value.strip()
    if entity_type == "ioc_domain":
        return looks_like_filename(v)
    if entity_type == "ioc_ip":
        return v in _BENIGN_PUBLIC_DNS or _is_benign_ipv4(v)
    return False


def select_targets(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [r for r in rows if is_noise(str(r["entity_type"]), str(r["value"]))]


def _run(apply: bool, repo: RunHistoryRepository | None = None) -> None:
    repo = repo if repo is not None else RunHistoryRepository()
    mode = "APPLY" if apply else "DRY-RUN"
    with repo._connect() as con:  # noqa: SLF001 — 修復スクリプト
        rows = con.execute(
            "SELECT ae.id AS eid, ae.entity_type, ae.value, a.title"
            " FROM article_entities ae JOIN articles a ON a.article_id = ae.article_id"
            " WHERE ae.entity_type IN ('ioc_domain','ioc_ip')"
        ).fetchall()
        targets = select_targets(rows)
        print(f"\n=== LLM 経由 IOC の誤りの掃除 ({mode}) ===")
        print(f"対象 {len(targets)} 行 / 走査 {len(rows)} 行:")
        for r in targets:
            print(f"  {r['entity_type']:10} {str(r['value'])[:40]:42} | {str(r['title'])[:50]}")
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
            print("\n(dry-run — --apply で実行。公開 DNS の行は見出しを目視)")
    _log.info("purge_llm_ioc_noise_done", apply=apply, targets=len(targets))


def main() -> None:
    ap = argparse.ArgumentParser(description="LLM 経由 IOC の誤りの掃除")
    ap.add_argument("--apply", action="store_true", help="実際に削除する (既定は dry-run)")
    _run(ap.parse_args().apply)


if __name__ == "__main__":
    main()
