"""主題アクターの LLM 層 backfill (2026-07-26)。

**動く統合判断分類器** を本文現存記事に流し、過去の本文主題 (title に名前が無く LLM 読解を
要する記事) を回収する。死んだ summarizer の再実行は無意味だが、judgment_classifier は
本文主題を正しく判定できる (実証済み) ため、この backfill は有意。

対象: 本文現存・非 ransomware・主題未確定・アクター言及ありの記事 (~993 件、実測 2026-07-26)。
取込と同一の判定ロジック (determine_subject_actors: title 層優先 → 分類器の主題を llm 層に
投入 → 言及所属ゲート) を再利用するため、ingest と結果が一致する。

usage:
    uv run python scripts/backfill_subject_llm.py            # dry-run (件数のみ)
    uv run python scripts/backfill_subject_llm.py --apply --limit 300
    uv run python scripts/backfill_subject_llm.py --apply    # 全件
"""

from __future__ import annotations

import argparse
import asyncio

from src.config_loader import load_app_config
from src.cti.actor_normalizer import load_actor_aliases
from src.cti.judgment_classifier import classify_judgment
from src.cti.subject_actor import SOURCE_LLM, determine_subject_actors
from src.pipeline.persistence import _relevant_actors
from src.storage.run_history import RunHistoryRepository
from src.tools.model_tiers import Step, build_llm_for


async def backfill(*, apply: bool, limit: int | None) -> dict[str, int]:
    repo = RunHistoryRepository()
    registry = load_actor_aliases()
    llm = build_llm_for(Step.ARTICLE_SUMMARY, load_app_config())

    lim_sql = f" LIMIT {int(limit)}" if limit else ""
    sql = (
        "SELECT DISTINCT a.article_id, a.title, a.body, a.category, MAX(a.created_at) ts "
        "FROM articles a "
        "JOIN article_entities ae ON ae.article_id=a.article_id "
        "  AND ae.entity_type IN ('actor','actor_provisional') "
        "WHERE a.body IS NOT NULL AND a.body <> '' "
        "AND a.feed_url NOT LIKE ? "
        "AND (a.subject_actor_ids IS NULL OR a.subject_actor_ids='') "
        "GROUP BY a.article_id, a.title, a.body, a.category "
        f"ORDER BY ts DESC{lim_sql}"
    )
    with repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute(sql, ("%ransomware.live%",)).fetchall()

    stats = {"scanned": 0, "title_hits": 0, "llm_hits": 0, "none": 0, "months": 0}
    touched: set[str] = set()
    from datetime import UTC

    from src.cti.actor_observed_history import month_label
    from src.storage.row_mappers import _from_iso

    for r in rows:
        stats["scanned"] += 1
        title = str(r["title"] or "")
        body = str(r["body"] or "")
        category = str(r["category"] or "") or None
        candidates = _relevant_actors(registry.find_all(body), category)
        # 統合分類器で主題を判定 (candidates=言及集合の二重ゲート)
        j = await classify_judgment(
            llm, title=title, category=category, body=body, published=None, candidates=candidates
        )
        classified_id = j.subject_actor_id if j else ""
        classified_conf = j.subject_confidence if j else "low"
        # recap (まとめ/調査) は単一主題を持たない — 分類器主題を捨てる (briefing と同ガード)
        if j and j.article_type == "recap":
            classified_id, classified_conf = "", "low"
        # 取込と同一の決定ロジック (title 層優先 → 分類器主題を llm 層へ)
        subj = determine_subject_actors(
            titles=(title,),
            detected_actor_ids=[c.id for c in candidates],
            llm_primary_actor_id=classified_id,
            llm_confidence=classified_conf,
            category=category,
            registry=registry,
        )
        if not subj.ids:
            stats["none"] += 1
            continue
        if subj.source == SOURCE_LLM:
            stats["llm_hits"] += 1
        else:
            stats["title_hits"] += 1
        if apply:
            repo.update_subject_actor_fields(
                str(r["article_id"]),
                ids_csv=",".join(subj.ids),
                source=subj.source,
                confidence=subj.confidence,
            )
        created = _from_iso(r["ts"])
        if created is not None:
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            touched.add(month_label(created))

    if apply and touched:
        from src.ui.services.actor_history_distill import distill_and_store

        distill_and_store(repo, sorted(touched))
        stats["months"] = len(touched)

    mode = "APPLIED" if apply else "DRY-RUN"
    print(f"{mode}: {stats}")
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(backfill(apply=args.apply, limit=args.limit))


if __name__ == "__main__":
    main()
