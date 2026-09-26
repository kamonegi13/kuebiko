"""指定したアクターの言及・主題を、今の辞書で記事から照合し直す (2026-09-27)。

辞書から別名を外したり曖昧指定を付けたりしても、過去の記事に付いた言及 (article_entities の actor)
と主題 (articles.subject_actor_ids) は残る。取込と同じ照合 (``ActorAliasRegistry.find_all``) を
見出し・要約・本文に当て、どれにも合わなくなった行だけを外す (退避表あり)。

既定 dry-run:
  docker exec kuebiko python -m scripts.revalidate_actor_mentions --actors apt37,andariel [--apply]
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from datetime import datetime

from src.cti.actor_normalizer import ActorAliasRegistry, load_actor_aliases
from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)


def still_matches(registry: ActorAliasRegistry, actor_id: str, texts: Iterable[str]) -> bool:
    """今の辞書で、どれかの本文にこのアクターが照合されるか。"""
    return any(a.id == actor_id for t in texts if t for a in registry.find_all(t))


def _run(actors: list[str], apply: bool, repo: RunHistoryRepository | None = None) -> None:
    repo = repo if repo is not None else RunHistoryRepository()
    registry = load_actor_aliases()
    # 時刻つき — 日付だけだと同じ日の再実行で前回の退避表を DROP で上書きしていた
    # (2026-09-27 に実際に発生)
    suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
    with repo._connect() as con:  # noqa: SLF001 — 修復スクリプト
        ph = ",".join("?" for _ in actors)
        mentions = con.execute(
            "SELECT e.id AS eid, e.value AS actor, a.title, a.summary, a.body"
            " FROM article_entities e JOIN articles a ON a.article_id = e.article_id"
            f" WHERE e.entity_type='actor' AND e.value IN ({ph})",  # noqa: S608 — ph は ? 固定
            actors,
        ).fetchall()
        drop_mentions = sorted(
            {
                int(r["eid"])
                for r in mentions
                if not still_matches(
                    registry,
                    str(r["actor"]),
                    (r["title"] or "", r["summary"] or "", r["body"] or ""),
                )
            }
        )
        subj_rows = con.execute(
            "SELECT id, subject_actor_ids, title, summary, body FROM articles"
            " WHERE subject_actor_ids IS NOT NULL AND subject_actor_ids <> ''"
        ).fetchall()
        subj_updates: list[tuple[int, str]] = []
        for r in subj_rows:
            ids = [x for x in str(r["subject_actor_ids"]).split(",") if x]
            texts = (r["title"] or "", r["summary"] or "", r["body"] or "")
            keep = [x for x in ids if x not in actors or still_matches(registry, x, texts)]
            if keep != ids:
                subj_updates.append((int(r["id"]), ",".join(keep)))
        print(f"=== 言及・主題の照合し直し ({'APPLY' if apply else 'DRY-RUN'}) — {actors} ===")
        print(f"言及: {len(mentions)} 行中 {len(drop_mentions)} 行が今の辞書で合わない")
        print(f"主題: {len(subj_updates)} 記事から外す")
        if not apply:
            print("(dry-run — --apply で実行)")
            return
        if drop_mentions:
            mph = ",".join("?" for _ in drop_mentions)
            bk = f"_backup_actor_revalidate_mentions_{suffix}"
            con.execute(f"DROP TABLE IF EXISTS {bk}")  # noqa: S608 — 固定名
            con.execute(
                f"CREATE TABLE {bk} AS SELECT * FROM article_entities WHERE id IN ({mph})",  # noqa: S608
                drop_mentions,
            )
            con.execute(f"DELETE FROM article_entities WHERE id IN ({mph})", drop_mentions)  # noqa: S608
        if subj_updates:
            bk = f"_backup_actor_revalidate_subjects_{suffix}"
            sph = ",".join("?" for _ in subj_updates)
            con.execute(f"DROP TABLE IF EXISTS {bk}")  # noqa: S608 — 固定名
            con.execute(
                f"CREATE TABLE {bk} AS SELECT id, article_id, subject_actor_ids FROM articles"  # noqa: S608
                f" WHERE id IN ({sph})",
                [i for i, _ in subj_updates],
            )
            for rid, new_ids in subj_updates:
                con.execute("UPDATE articles SET subject_actor_ids=? WHERE id=?", (new_ids, rid))
        print("適用しました (退避表 _backup_actor_revalidate_*_" + suffix + ")")
    _log.info("revalidate_actor_mentions_done", actors=actors, mentions=len(drop_mentions))


def main() -> None:
    ap = argparse.ArgumentParser(description="アクターの言及・主題を今の辞書で照合し直す")
    ap.add_argument("--actors", required=True, help="カンマ区切りの actor id")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    _run([x.strip() for x in args.actors.split(",") if x.strip()], args.apply)


if __name__ == "__main__":
    main()
