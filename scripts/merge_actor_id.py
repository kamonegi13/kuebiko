"""actor id の merge に伴う過去データ remap (2026-08-01 thegentlemen 事故の修復)。

辞書側で from_id を墓標化 (status: merged, merged_into: to_id) した後に実行し、
DB 上の過去帰属を継承先へ寄せる:

1. articles.subject_actor_ids — CSV 内の from_id トークンを to_id に置換 + 重複除去
   (``the_gentlemen,thegentlemen`` の二重付与 → ``the_gentlemen``)
2. article_entities (entity_type='actor') — value の remap。同一記事に to_id 行が
   既にあれば from_id 行を DELETE (UNIQUE 衝突 dedup)、無ければ UPDATE
3. actor_observed_profile — from_id の月次行を DELETE し、影響月を再蒸留
   (distill_and_store は月単位全置換なので remap 済み articles から正しく再構築される)

llm_primary_actor_raw は「辞書解決前の生入力を不改変で残す」設計 (D1) のため触らない。

既定 dry-run。--apply を付けると実行:
  docker exec kuebiko python -m scripts.merge_actor_id \
      --from thegentlemen --to the_gentlemen [--apply]
"""

from __future__ import annotations

import argparse

from src.cti.actor_normalizer import load_actor_aliases
from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)


def _validate_dictionary_state(from_id: str, to_id: str) -> None:
    """辞書が merge 済み状態 (from=墓標 or 不在、to=active、redirect 整合) か検証する。"""
    registry = load_actor_aliases()
    target = registry.by_id(to_id)
    if target is None or target.is_merged:
        raise SystemExit(f"継承先 {to_id} が辞書に active で存在しません — 先に辞書を確認")
    source = registry.by_id(from_id)
    if source is not None:
        if not source.is_merged:
            raise SystemExit(
                f"{from_id} がまだ active です — 先に辞書側で墓標化 (status: merged) してください"
            )
        resolved = registry.resolve_actor_id(from_id)
        if resolved != to_id:
            raise SystemExit(f"{from_id} の redirect 先が {resolved} で {to_id} と不一致")


def _remap_csv(csv: str, from_id: str, to_id: str) -> str:
    """subject_actor_ids CSV の from_id を to_id に置換し、順序保持で重複除去する。"""
    out: list[str] = []
    for token in (t.strip() for t in csv.split(",")):
        if not token:
            continue
        mapped = to_id if token == from_id else token
        if mapped not in out:
            out.append(mapped)
    return ",".join(out)


def _run(from_id: str, to_id: str, apply: bool, repo: RunHistoryRepository | None = None) -> None:
    _validate_dictionary_state(from_id, to_id)
    repo = repo if repo is not None else RunHistoryRepository()
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"\n=== actor id merge remap: {from_id} -> {to_id} ({mode}) ===")

    with repo._connect() as con:  # noqa: SLF001 — 修復スクリプト
        # 1. subject_actor_ids CSV remap
        rows = con.execute(
            "SELECT article_id, subject_actor_ids FROM articles WHERE subject_actor_ids LIKE ?",
            (f"%{from_id}%",),
        ).fetchall()
        subject_updates = 0
        for r in rows:
            old = str(r["subject_actor_ids"] or "")
            tokens = [t.strip() for t in old.split(",")]
            if from_id not in tokens:
                continue  # LIKE の部分一致 (別 id の内包) は対象外
            new = _remap_csv(old, from_id, to_id)
            if new == old:
                continue
            subject_updates += 1
            if apply:
                con.execute(
                    "UPDATE articles SET subject_actor_ids=? WHERE article_id=?",
                    (new, str(r["article_id"])),
                )
        print(f"subject_actor_ids remap : {subject_updates} 行")

        # 2. article_entities remap (to_id 行が既にある記事は from_id 行を dedup DELETE)
        collide = (
            "EXISTS (SELECT 1 FROM article_entities e2"
            " WHERE e2.article_id=article_entities.article_id"
            " AND e2.entity_type='actor' AND e2.value=?)"
        )
        if apply:
            deduped = con.execute(
                f"DELETE FROM article_entities WHERE entity_type='actor' AND value=? AND {collide}",
                (from_id, to_id),
            ).rowcount
            updated = con.execute(
                "UPDATE article_entities SET value=? WHERE entity_type='actor' AND value=?",
                (to_id, from_id),
            ).rowcount
        else:
            deduped = int(
                con.execute(
                    "SELECT COUNT(*) AS n FROM article_entities"
                    f" WHERE entity_type='actor' AND value=? AND {collide}",
                    (from_id, to_id),
                ).fetchone()["n"]
                or 0
            )
            updated = int(
                con.execute(
                    "SELECT COUNT(*) AS n FROM article_entities"
                    f" WHERE entity_type='actor' AND value=? AND NOT {collide}",
                    (from_id, to_id),
                ).fetchone()["n"]
                or 0
            )
        print(f"entities dedup DELETE   : {deduped} 行 / UPDATE: {updated} 行")

        # 3. actor_observed_profile の from_id 行 (影響月の把握 + 削除)
        months = [
            str(r["month"])
            for r in con.execute(
                "SELECT DISTINCT month FROM actor_observed_profile WHERE actor_id=?",
                (from_id,),
            ).fetchall()
        ]
        if apply and months:
            con.execute("DELETE FROM actor_observed_profile WHERE actor_id=?", (from_id,))
        print(f"observed_profile 削除   : {from_id} の {len(months)} か月分 {sorted(months)}")

    # 4. 影響月の再蒸留 (remap 済み articles から to_id 側を正しく再構築)
    if apply and months:
        from src.ui.services.actor_history_distill import distill_and_store

        stats = distill_and_store(repo, sorted(months))
        print(f"再蒸留                  : {stats}")
    if not apply:
        print("(dry-run — --apply で実行)")
    _log.info(
        "merge_actor_id_done",
        from_id=from_id,
        to_id=to_id,
        apply=apply,
        subject_updates=subject_updates,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="actor id merge の過去データ remap")
    ap.add_argument("--from", dest="from_id", required=True, help="墓標化した旧 id")
    ap.add_argument("--to", dest="to_id", required=True, help="継承先の active id")
    ap.add_argument("--apply", action="store_true", help="実際に更新する (既定は dry-run)")
    args = ap.parse_args()
    _run(args.from_id, args.to_id, args.apply)


if __name__ == "__main__":
    main()
