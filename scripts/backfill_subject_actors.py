"""主題アクター title 層の backfill (決定論のみ、LLM なし)。

docs/subject_actor_attribution_design.md §3。subject_actor_source が NULL (未評価) の
posted 記事に対し **タイトル alias 走査 (第 1 層) のみ** を適用する:

- タイトルにアクター名がヒット → subject_actor_source='title' + ids を書込
- ヒットなし → NULL のまま温存 (legacy 照合継続)。歴史行の routing_flags
  (LLM primary_actor_id) は未永続で第 2 層を遡及できないため、'none' を書くと
  正当な言及照合まで失われる — 意図的に触らない (設計 §4 の残余)。

実行後、entity_type='pir' タグへ反映するには rebuild_pir_entities を回す
(夜間 job で自動、即時反映は --rebuild-pir)。

usage:
    uv run python scripts/backfill_subject_actors.py            # dry-run (既定)
    uv run python scripts/backfill_subject_actors.py --apply
    uv run python scripts/backfill_subject_actors.py --apply --rebuild-pir
"""

from __future__ import annotations

import argparse

from src.cti.actor_normalizer import load_actor_aliases
from src.cti.subject_actor import SOURCE_TITLE, determine_subject_actors
from src.storage.run_history import RunHistoryRepository

DEFAULT_WINDOW_DAYS = 90


def backfill(*, window_days: int, apply: bool) -> dict[str, int]:
    """title 層 backfill 本体。返り値 = 集計 counts。"""
    repo = RunHistoryRepository()
    registry = load_actor_aliases()
    with repo._connect() as conn:  # noqa: SLF001 (intra-tool script)
        rows = conn.execute(
            """
            SELECT DISTINCT article_id, title, category
              FROM articles
             WHERE status='posted'
               AND subject_actor_source IS NULL
               AND datetime(created_at) >= datetime('now', ?)
            """,
            (f"-{window_days} days",),
        ).fetchall()

    scanned = 0
    hits: list[tuple[str, str]] = []  # (article_id, ids_csv)
    samples: list[str] = []
    for r in rows:
        scanned += 1
        subj = determine_subject_actors(
            titles=(str(r["title"] or ""),),
            detected_actor_ids=(),
            llm_primary_actor_id="",  # 第 2 層は backfill 対象外 (遡及不能)
            llm_confidence="low",
            category=str(r["category"] or ""),
            registry=registry,
        )
        if subj.source == SOURCE_TITLE and subj.ids:
            hits.append((str(r["article_id"]), ",".join(subj.ids)))
            if len(samples) < 15:
                samples.append(f"  {','.join(subj.ids):40s} | {str(r['title'])[:70]}")

    if apply and hits:
        with repo._connect() as conn:  # noqa: SLF001
            for article_id, ids_csv in hits:
                conn.execute(
                    "UPDATE articles SET subject_actor_ids=?, subject_actor_source=? "
                    "WHERE article_id=?",
                    (ids_csv, SOURCE_TITLE, article_id),
                )

    print(f"scanned (subject 未評価の posted, {window_days}d): {scanned}")
    print(f"title 層ヒット: {len(hits)}  ({'書込済' if apply else 'dry-run — 書込なし'})")
    print("samples:")
    print("\n".join(samples))
    return {"scanned": scanned, "hits": len(hits)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument("--apply", action="store_true", help="実際に UPDATE する (既定 dry-run)")
    parser.add_argument(
        "--rebuild-pir",
        action="store_true",
        help="backfill 後に entity_type='pir' タグを full rebuild する (apply 時のみ)",
    )
    args = parser.parse_args()
    backfill(window_days=args.days, apply=args.apply)
    if args.apply and args.rebuild_pir:
        from src.pir.persist import rebuild_pir_entities

        result = rebuild_pir_entities()
        print(f"pir entities rebuilt: {result}")


if __name__ == "__main__":
    main()
