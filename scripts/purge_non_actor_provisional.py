"""非アクター汚染の掃除 (カテゴリ混同監査 2026-08-13 の是正バッチ)。

named_primary_actor 経由で actor_provisional (暗定アクター候補) と新興アクター提案
キューに混入した非アクター (ツール/人物/企業/AI モデル名/地政学ノイズ) を、
取込フィルタと同じ SSoT 述語 (``is_known_non_actor`` + ``is_geopolitical_noise``) で
判定して除去する。上流フィルタ (actor_candidates) の導入と同時に過去蓄積を掃除する —
「供給し続ける上流には取込 filter 同時導入で除去完結」の原則。

対象:
- article_entities の entity_type='actor_provisional' 行 (検索 facet に即可視のため実害大)
- actor_update_proposals の status='pending' かつ proposal_type='corpus_emerging_actor'
  (該当は rejected へ倒す — dedup_key により同一候補は再提案されない)

実行 (コンテナ内で):
    docker exec -i kuebiko python - < scripts/purge_non_actor_provisional.py            # dry-run
    docker exec -i kuebiko python - --apply < scripts/purge_non_actor_provisional.py    # 実行
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from src.cti.actor_candidates import (
    is_geopolitical_noise,
    is_known_non_actor,
    normalize_actor_key,
)
from src.cti.actor_normalizer import load_actor_aliases
from src.storage.run_history import RunHistoryRepository


def main() -> None:
    apply_mode = "--apply" in sys.argv[1:]
    repo = RunHistoryRepository(db_path=Path("/app/data/run_history.db"))
    reg = load_actor_aliases()

    def _is_noise(name: str) -> bool:
        key = normalize_actor_key(name)
        return bool(key) and (is_known_non_actor(key, reg) or is_geopolitical_noise(key))

    # ---- 1. actor_provisional entity ----
    with repo._connect() as conn:  # noqa: SLF001 — 運用バッチは repo 接続を直接使う
        rows = conn.execute(
            "SELECT value, COUNT(*) AS n FROM article_entities"
            " WHERE entity_type='actor_provisional' GROUP BY value"
        ).fetchall()
    prov_targets = [(str(r["value"]), int(r["n"])) for r in rows if _is_noise(str(r["value"]))]
    prov_keep = len(rows) - len(prov_targets)

    print(f"actor_provisional: 対象 {len(prov_targets)} 値 / 維持 {prov_keep} 値")
    for v, n in sorted(prov_targets, key=lambda x: -x[1]):
        print(f"  削除候補: {v} ({n} 行)")

    # ---- 2. pending 新興アクター提案 ----
    proposals = repo.list_actor_update_proposals(status="pending")
    prop_targets: list[tuple[int, str]] = []
    for p in proposals:
        if p.proposal_type != "corpus_emerging_actor":
            continue
        try:
            payload = json.loads(p.payload)
        except (TypeError, ValueError):
            continue
        name = str(payload.get("canonical") or payload.get("id") or "").strip()
        if name and _is_noise(name):
            prop_targets.append((p.id, name))

    print(f"\npending 提案 (corpus_emerging_actor): 却下候補 {len(prop_targets)} 件")
    for pid, name in prop_targets:
        print(f"  却下候補: #{pid} {name}")

    if not apply_mode:
        print("\n(dry-run: 変更していません。--apply で実行)")
        return

    deleted = 0
    with repo._connect() as conn:  # noqa: SLF001
        for v, _n in prov_targets:
            cur = conn.execute(
                "DELETE FROM article_entities WHERE entity_type='actor_provisional' AND value=?",
                (v,),
            )
            deleted += int(cur.rowcount or 0)
    rejected = 0
    for pid, _name in prop_targets:
        if repo.decide_actor_update_proposal(pid, status="rejected"):
            rejected += 1
    print(f"\n削除完了: provisional {deleted} 行 / 提案却下 {rejected} 件")


if __name__ == "__main__":
    main()
