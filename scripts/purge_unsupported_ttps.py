"""本文で裏付けられない TTP の掃除 (2026-09-27)。

取込の関門 (``src.cti.ttp_evidence.filter_llm_techniques``) と **同じ照合** で過去分を掃除する。
LLM の TTP は本文で裏付けられるのが 3-4 割 (Opus 盲検 160 組: 全部残すと精度 0.31、
照合で残すと精度 0.70 / 回収 0.70)。

- 本文 (title / body / body_ja) が空の記事は判定できないので残す (本文を捨てた古い記事)
- 本文の T 番号 (正規表現経路) は照合の ``id`` で必ず残る
- 削除する行は時刻つきの退避表へ複製してから消す (同じ日に 2 回流しても上書きしない)

既定 dry-run。--apply で実行:
  docker exec -w /app kuebiko python -m scripts.purge_unsupported_ttps [--apply]
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from src.cti.ttp_evidence import find_ttp_evidence
from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)

_CHUNK = 500


def _texts(con: Any, article_ids: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i in range(0, len(article_ids), _CHUNK):
        chunk = article_ids[i : i + _CHUNK]
        ph = ",".join("?" for _ in chunk)
        rows = con.execute(
            "SELECT article_id, title, body, body_ja FROM articles"  # noqa: S608 — ph は ? 固定
            f" WHERE article_id IN ({ph})",
            chunk,
        ).fetchall()
        for r in rows:
            parts = [str(r[k] or "") for k in ("body", "body_ja")]
            if not any(p.strip() for p in parts):
                continue  # 本文が無い行 (同じ記事の別行に本文があればそちらを使う)
            out.setdefault(str(r["article_id"]), "\n".join([str(r["title"] or ""), *parts]))
    return out


def select_targets(rows: list[dict[str, Any]], texts: dict[str, str]) -> list[dict[str, Any]]:
    """本文があって、照合で裏付けが見つからない TTP 行。"""
    return [
        r
        for r in rows
        if r["article_id"] in texts
        and find_ttp_evidence(str(r["value"]), texts[r["article_id"]]) is None
    ]


def _run(apply: bool, repo: RunHistoryRepository | None = None) -> None:
    repo = repo if repo is not None else RunHistoryRepository()
    mode = "APPLY" if apply else "DRY-RUN"
    with repo._connect() as con:  # noqa: SLF001 — 修復スクリプト
        rows = [
            dict(r)
            for r in con.execute(
                "SELECT id AS eid, article_id, value FROM article_entities WHERE entity_type='ttp'"
            ).fetchall()
        ]
        texts = _texts(con, sorted({r["article_id"] for r in rows}))
        targets = select_targets(rows, texts)
        judged = sum(1 for r in rows if r["article_id"] in texts)
        print(f"\n=== 本文で裏付けられない TTP の掃除 ({mode}) ===")
        print(f"走査 {len(rows)} 行 / 本文あり {judged} 行 / 対象 {len(targets)} 行")
        for tid, n in Counter(str(r["value"]) for r in targets).most_common(15):
            kept = sum(1 for r in rows if r["value"] == tid) - n
            print(f"  {tid:10} 削除 {n:5d} / 残す {kept:5d}")
        if not targets or not apply:
            print("\n(dry-run — --apply で実行)" if targets else "(対象なし)")
            return
        backup = "_backup_unsupported_ttp_" + datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        con.execute(
            f"CREATE TABLE {backup} AS SELECT * FROM article_entities WHERE 1=0"  # noqa: S608
        )
        deleted = 0
        ids = [int(r["eid"]) for r in targets]
        for i in range(0, len(ids), _CHUNK):
            chunk = ids[i : i + _CHUNK]
            ph = ",".join("?" for _ in chunk)
            con.execute(
                f"INSERT INTO {backup} SELECT * FROM article_entities WHERE id IN ({ph})",  # noqa: S608
                chunk,
            )
            deleted += con.execute(
                f"DELETE FROM article_entities WHERE id IN ({ph})",  # noqa: S608 — ph は ? 固定
                chunk,
            ).rowcount
        print(f"\nDELETE {deleted} 行 (退避 = {backup})")
    _log.info("purge_unsupported_ttps_done", apply=apply, targets=len(targets))


def main() -> None:
    ap = argparse.ArgumentParser(description="本文で裏付けられない TTP の掃除")
    ap.add_argument("--apply", action="store_true", help="実際に削除する (既定は dry-run)")
    _run(ap.parse_args().apply)


if __name__ == "__main__":
    main()
