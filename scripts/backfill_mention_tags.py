"""既存記事への言及タグ backfill (mentioned_country / campaign、決定論・LLM 不使用)。

mention_tagger の ingest 配線は新着にしか効かないため、既存の posted 記事全件に対して
title+summary から言及タグを導出して article_entities に追記する (INSERT OR IGNORE = 冪等)。
entity の created_at は記事の created_at に合わせる (時間窓クエリとの整合)。

使い方 (本番はコンテナ内で実行 — PG は DATABASE_URL 経由):
    uv run python scripts/backfill_mention_tags.py --dry-run --sample 50   # 精度サンプル
    docker exec kuebiko python scripts/backfill_mention_tags.py --apply
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from src.cti.mention_tagger import derive_mention_entities
from src.storage.run_history import RunHistoryRepository

_BATCH = 500


def _load_articles(repo: RunHistoryRepository) -> list[tuple[str, str, str, str]]:
    """posted 記事の (article_id, title, summary, created_at) を article_id 一意で返す。"""
    with repo._connect() as conn:  # noqa: SLF001 — 接続 seam の意図的共有 (situation_store と同型)
        rows = conn.execute(
            """
            SELECT article_id, MAX(created_at) AS created_at,
                   MAX(title) AS title, MAX(COALESCE(summary, '')) AS summary
              FROM articles
             WHERE status='posted'
             GROUP BY article_id
            """
        ).fetchall()
    return [
        (str(r["article_id"]), str(r["title"] or ""), str(r["summary"] or ""), str(r["created_at"]))
        for r in rows
    ]


def _involved_isos(repo: RunHistoryRepository, article_ids: list[str]) -> dict[str, set[str]]:
    """article_id → involved_country ISO (小文字) をバッチ取得。"""
    out: dict[str, set[str]] = {}
    for i in range(0, len(article_ids), _BATCH):
        chunk = article_ids[i : i + _BATCH]
        keys = repo.entity_keys_for_articles(chunk, types=("involved_country",))
        for aid, ks in keys.items():
            out[aid] = {k.split(":", 1)[1].lower() for k in ks if ":" in k}
    return out


def run(*, apply: bool, sample: int) -> None:
    repo = RunHistoryRepository()
    articles = _load_articles(repo)
    involved = _involved_isos(repo, [a[0] for a in articles])

    total_articles = len(articles)
    tagged_articles = 0
    total_tags = 0
    campaign_tags = 0
    shown = 0
    for aid, title, summary, created_at in articles:
        ents = derive_mention_entities(
            title=title, summary=summary, involved_isos=involved.get(aid, set())
        )
        if not ents:
            continue
        tagged_articles += 1
        total_tags += len(ents)
        campaign_tags += sum(1 for t, _ in ents if t == "campaign")
        if not apply and shown < sample:
            shown += 1
            vals = ", ".join(f"{t}:{v}" for t, v in ents)
            print(f"[{vals}] <- {title[:80]}")
        if apply:
            try:
                when = datetime.fromisoformat(created_at)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
            except ValueError:
                when = datetime.now(UTC)
            repo.add_article_entities(aid, list(ents), when=when)

    mode = "APPLIED" if apply else "DRY-RUN"
    print(
        f"\n{mode}: articles={total_articles} tagged={tagged_articles} "
        f"({tagged_articles / max(1, total_articles):.0%}) tags={total_tags} "
        f"(campaign={campaign_tags})"
    )


def main() -> None:
    p = argparse.ArgumentParser(description="mentioned_country / campaign の backfill")
    p.add_argument("--apply", action="store_true", help="実際に書き込む (既定は dry-run)")
    p.add_argument("--dry-run", action="store_true", help="導出のみ (既定)")
    p.add_argument("--sample", type=int, default=50, help="dry-run で表示するサンプル数")
    args = p.parse_args()
    run(apply=bool(args.apply and not args.dry_run), sample=args.sample)


if __name__ == "__main__":
    main()
