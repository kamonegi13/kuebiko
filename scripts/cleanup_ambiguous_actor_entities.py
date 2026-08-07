"""曖昧アクターの誤検出 entity クリーンアップ (docs/actor_identity_cue_design.md §3.3)。

ジャンル cue 時代に登録された article_entities (type=actor) を同一性 cue (E0-E7) で
再評価し、証拠の無い行を削除する。既定は dry-run (件数レポート + 削除候補を JSONL 出力
→ 正当言及の取りこぼし率をサンプル検証してから --apply)。

実行 (host から):
    DATABASE_URL=postgresql://kuebiko:<pw>@127.0.0.1:5433/kuebiko \
      uv run python scripts/cleanup_ambiguous_actor_entities.py [--apply]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.cti.actor_normalizer import has_identity_evidence, load_actor_aliases
from src.storage.run_history import RunHistoryRepository

REPORT = Path("data/ambiguous_actor_cleanup_report.jsonl")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="削除を実行 (省略時 dry-run)")
    args = ap.parse_args()

    registry = load_actor_aliases()
    ambiguous = [a for a in registry.actors if a.ambiguous and not a.is_merged]
    repo = RunHistoryRepository()
    report_rows: list[dict[str, object]] = []
    total_keep = total_drop = 0

    print(f"{'actor':12} {'total':>6} {'keep':>6} {'drop':>6}")
    for actor in ambiguous:
        with repo._connect() as conn:
            rows = conn.execute(
                """SELECT e.id, e.article_id, a.title, COALESCE(a.body,'')
                   FROM article_entities e
                   JOIN articles a ON a.article_id = e.article_id
                   WHERE e.entity_type = 'actor' AND lower(e.value) = ?""",
                (actor.id,),
            ).fetchall()
        drop_ids: list[int] = []
        for row_id, article_id, title, body in rows:
            # title を先頭に含める (E6 被害者レコード形式は title で判定される)
            text = f"{title or ''}\n{(body or '')[:40000]}"
            if not has_identity_evidence(actor, actor.canonical, text):
                drop_ids.append(row_id)
                report_rows.append(
                    {"actor": actor.id, "article_id": article_id, "title": (title or "")[:120]}
                )
        keep = len(rows) - len(drop_ids)
        total_keep += keep
        total_drop += len(drop_ids)
        print(f"{actor.id:12} {len(rows):>6} {keep:>6} {len(drop_ids):>6}")
        if args.apply and drop_ids:
            with repo._connect() as conn:
                for row_id in drop_ids:
                    conn.execute("DELETE FROM article_entities WHERE id = ?", (row_id,))

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in report_rows), encoding="utf-8"
    )
    mode = "APPLIED (削除済)" if args.apply else "DRY-RUN (未削除)"
    print(f"\n{mode}: keep={total_keep} drop={total_drop} → 削除候補一覧 {REPORT}")


if __name__ == "__main__":
    main()
