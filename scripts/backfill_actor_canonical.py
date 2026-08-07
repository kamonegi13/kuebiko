"""mention actor entity のスラグ綴りを canonical id に正規化する backfill (2026-07-27, 案A)。

docs/body_extraction_and_entity_integrity_redesign.md §5。

article_entities の entity_type='actor' の value に、スラグ綴り (例 'thegentlemen',
'akira') と canonical 辞書 id (例 'the_gentlemen', 'akira_ransom') が混在している。
これは (a) subject (canonical) と mention (slug) が別 id になり subject-gate が正当な
自己帰属を誤ドロップする、(b) 検索 facet / 地図 / 概況で同一アクターが二重計上される、を招く。

各 mention value を resolve_source_slug → by_id(resolve_actor_id) で canonical に解決し、
canonical と異なるものを remap する。UNIQUE(article,type,value) 衝突は既存 canonical 行が
あればスラグ行を DELETE (dedup)、無ければ value を UPDATE する。

**前提**: この backfill の前に repo_articles.ransomware_live_group_names の散文展開修正
(auto-ransom-dict の回帰防止) を deploy しておくこと。

既定 dry-run。--apply で実行。
  docker exec kuebiko python -m scripts.backfill_actor_canonical            # dry-run
  docker exec kuebiko python -m scripts.backfill_actor_canonical --apply    # 実行
"""

from __future__ import annotations

import argparse

from src.cti.actor_normalizer import load_actor_aliases
from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)


def _canonical_for(value: str, registry: object) -> str:
    """mention value → canonical id (解決できなければ value のまま)。"""
    reg = registry
    entry = reg.resolve_source_slug(value) or reg.by_id(reg.resolve_actor_id(value))  # type: ignore[attr-defined]
    return entry.id if entry is not None else value


def _run(apply: bool) -> None:
    repo = RunHistoryRepository()
    registry = load_actor_aliases()
    with repo._connect() as con:  # noqa: SLF001 — backfill スクリプト
        values = [
            str(r["value"])
            for r in con.execute(
                "SELECT DISTINCT value FROM article_entities WHERE entity_type='actor'"
            ).fetchall()
        ]
        remaps = [(v, _canonical_for(v, registry)) for v in values]
        remaps = [(v, c) for v, c in remaps if c != v]

        print(f"\n=== mention actor 正規化 backfill ({'APPLY' if apply else 'DRY-RUN'}) ===")
        print(f"distinct actor values : {len(values)}")
        print(f"remap 対象            : {len(remaps)}")
        total_updated = 0
        total_deduped = 0
        for slug, canon in sorted(remaps):
            # 衝突: 同一記事に既に canonical 行がある slug 行は削除 (dedup)
            deduped = con.execute(
                "DELETE FROM article_entities WHERE entity_type='actor' AND value=? "
                "AND EXISTS (SELECT 1 FROM article_entities e2 "
                "  WHERE e2.article_id=article_entities.article_id "
                "  AND e2.entity_type='actor' AND e2.value=?)",
                (slug, canon),
            ).rowcount if apply else _count_collisions(con, slug, canon)
            # 残りは value を canonical へ UPDATE
            updated = con.execute(
                "UPDATE article_entities SET value=? WHERE entity_type='actor' AND value=?",
                (canon, slug),
            ).rowcount if apply else _count_plain(con, slug, canon)
            total_updated += int(updated or 0)
            total_deduped += int(deduped or 0)
            print(f"  {slug:22} -> {canon:20}  update={updated}  dedup_delete={deduped}")
        print(f"\n合計: UPDATE {total_updated} 行 / DEDUP DELETE {total_deduped} 行")
        if not apply:
            print("(dry-run — --apply で実行)")
    _log.info("actor_canonical_backfill_done", apply=apply, remaps=len(remaps))


_COLLIDE = (
    "EXISTS (SELECT 1 FROM article_entities e2 WHERE e2.article_id=article_entities.article_id"
    " AND e2.entity_type='actor' AND e2.value=?)"
)


def _count_collisions(con: object, slug: str, canon: str) -> int:
    row = con.execute(  # type: ignore[attr-defined]
        "SELECT COUNT(*) AS n FROM article_entities "
        f"WHERE entity_type='actor' AND value=? AND {_COLLIDE}",
        (slug, canon),
    ).fetchone()
    return int(row["n"] or 0)


def _count_plain(con: object, slug: str, canon: str) -> int:
    row = con.execute(  # type: ignore[attr-defined]
        "SELECT COUNT(*) AS n FROM article_entities "
        f"WHERE entity_type='actor' AND value=? AND NOT {_COLLIDE}",
        (slug, canon),
    ).fetchone()
    return int(row["n"] or 0)


def main() -> None:
    ap = argparse.ArgumentParser(description="mention actor を canonical に正規化する backfill")
    ap.add_argument("--apply", action="store_true", help="実際に更新する (既定は dry-run)")
    args = ap.parse_args()
    _run(args.apply)


if __name__ == "__main__":
    main()
