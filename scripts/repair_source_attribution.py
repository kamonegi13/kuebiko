"""R2 遡及修復 (2026-07-26): source 断言の遡及付与 + 辞書外 actor entity の provisional 化。

ransomware.live 等が briefing 迂回で書いた過去記事を新機構に合わせて修復する:
  1. source slug 解決できるグループの記事 → subject_actor_ids を source 断言で遡及付与
     (thegentlemen / incransom / akira 等)。既存 subject は保持 (id 追加のみ)。
  2. 辞書外の 'actor' 生書き entity → 'actor_provisional' へ移行 (deadlock/nova 等)。
     これで propose_emerging_actors の収穫網に乗り人承認キューに現れる。

冪等: 再実行しても二重付与しない。dry-run 既定。

usage:
    uv run python scripts/repair_source_attribution.py            # dry-run
    uv run python scripts/repair_source_attribution.py --apply
"""

from __future__ import annotations

import argparse

from src.cti.actor_normalizer import load_actor_aliases
from src.cti.subject_actor import SOURCE_FEED
from src.storage.run_history import RunHistoryRepository


def repair(*, apply: bool) -> dict[str, int]:
    repo = RunHistoryRepository()
    registry = load_actor_aliases()
    stats = {"subject_backfilled": 0, "entity_migrated": 0, "unknown_slugs": 0}

    with repo._connect() as conn:  # noqa: SLF001 (intra-tool script)
        # ---- 1. 辞書外 'actor' entity → provisional 移行 + source 断言の遡及 ----
        # ransomware.live 記事 (feed_title で識別) の actor entity を対象にする
        rows = conn.execute(
            "SELECT DISTINCT ae.article_id, ae.value AS actor "
            "FROM article_entities ae "
            "JOIN articles a ON a.article_id = ae.article_id "
            "WHERE ae.entity_type = 'actor' AND a.feed_url LIKE '%ransomware.live%'"
        ).fetchall()

    unknown_seen: set[str] = set()
    subject_writes: list[tuple[str, str]] = []  # (article_id, actor_id)
    entity_migrations: list[tuple[str, str]] = []  # (article_id, raw_value)
    for r in rows:
        aid = str(r["article_id"])
        raw = str(r["actor"])
        hit = registry.resolve_source_slug(raw) or registry.by_id(raw)
        if hit is not None:
            subject_writes.append((aid, hit.id))
        else:
            entity_migrations.append((aid, raw))
            unknown_seen.add(raw)
    stats["unknown_slugs"] = len(unknown_seen)

    if apply:
        with repo._connect() as conn:  # noqa: SLF001
            for aid, actor_id in subject_writes:
                # 既存 subject を保持して id 追加 (既に含むなら skip)
                cur = conn.execute(
                    "SELECT subject_actor_ids FROM articles WHERE article_id = ? LIMIT 1",
                    (aid,),
                ).fetchone()
                existing = [
                    s.strip()
                    for s in str((cur["subject_actor_ids"] if cur else "") or "").split(",")
                    if s.strip()
                ]
                if actor_id in existing:
                    continue
                new_csv = ",".join([*existing, actor_id]) if existing else actor_id
                conn.execute(
                    "UPDATE articles SET subject_actor_ids = ?, subject_actor_source = ? "
                    "WHERE article_id = ?",
                    (new_csv, SOURCE_FEED, aid),
                )
                stats["subject_backfilled"] += 1
            for aid, raw in entity_migrations:
                conn.execute(
                    "UPDATE article_entities SET entity_type = 'actor_provisional' "
                    "WHERE article_id = ? AND entity_type = 'actor' AND value = ?",
                    (aid, raw),
                )
                stats["entity_migrated"] += 1
    else:
        stats["subject_backfilled"] = len(subject_writes)
        stats["entity_migrated"] = len(entity_migrations)

    print(f"{'APPLIED' if apply else 'DRY-RUN'}: {stats}")
    print(f"未知グループ (provisional 化 → 人承認候補): {sorted(unknown_seen)[:30]}")
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="実際に UPDATE する (既定 dry-run)")
    args = parser.parse_args()
    repair(apply=args.apply)


if __name__ == "__main__":
    main()
