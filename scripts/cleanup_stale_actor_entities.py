"""actor entity 残骸掃除の運用バッチ (幽霊アクター監査 2026-08-06)。

ロジック本体は src/ui/services/entity_retro_cleanup.py (辞書編集フックと共有)。
本スクリプトは全 actor 一括の CLI ラッパで、既定は dry-run。

実行 (コンテナ内で):
    docker exec -i kuebiko python - < scripts/cleanup_stale_actor_entities.py            # dry-run
    docker exec -i kuebiko python - --apply < scripts/cleanup_stale_actor_entities.py    # 削除実行
"""

from __future__ import annotations

import sys
from pathlib import Path

from src.storage.run_history import RunHistoryRepository
from src.ui.services.entity_retro_cleanup import cleanup_stale_actor_entities


def main() -> None:
    apply_mode = "--apply" in sys.argv[1:]
    repo = RunHistoryRepository(db_path=Path("/app/data/run_history.db"))
    stats = cleanup_stale_actor_entities(repo, apply=apply_mode)

    print(f"対象 (article, actor) 対: {stats.checked}")
    print(f"  照合成立 (維持): {stats.kept_matched}")
    print(f"  構造化ソース由来 (維持): {stats.kept_structured}")
    print(f"  本文なし・検証不能 (維持): {stats.kept_unverifiable}")
    print(f"  削除候補: {len(stats.delete_candidates)}")
    print("\n削除候補の actor 別内訳:")
    for actor, n in sorted(stats.deleted_by_actor.items(), key=lambda x: -x[1])[:30]:
        print(f"  {actor:24s} {n}")
    print("\n削除候補サンプル (先頭 15):")
    for aid, val, reason in stats.delete_candidates[:15]:
        print(f"  [{val}] {reason} {aid[:70]}")

    if apply_mode:
        print(f"\n削除完了: {stats.deleted} 行")
    else:
        print("\n(dry-run: 削除していません。--apply で実行)")


if __name__ == "__main__":
    main()
