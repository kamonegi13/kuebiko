#!/usr/bin/env python3
"""新規追加アクター (既定=同盟・関係国) を既存コーパスへ遡及検出する R-A ゲート付き backfill。

actor 検出は ingestion 時に走るため、後から actor_aliases.yaml に追加したアクター
(同盟側 +17 等) は既存記事にタグされない。本 script は既存 posted 記事を再スキャンし、
**対象アクターのみ** を `_relevant_actors` (R-A) ゲート付きで article_entities へ追記する。

backfill_article_entities.py (全 actor を gate 無しで再検出) と異なり:
- 対象を限定 (既定: 同盟・関係国 nation) → 既存 adversary タグを攪乱しない
- R-A ゲート適用 (kind=group=常時 / organization=cyber カテゴリのみ) → 過剰帰属を防ぐ
- INSERT OR IGNORE (冪等)

Usage:
    docker exec kuebiko /app/.venv/bin/python3 -m scripts.redetect_actor_entities [--apply] \
        [--ids id1,id2]   # 未指定なら同盟 nation (us/gb/il/fr/de/au/ca/nz/kr/in) 全件
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.cti.actor_normalizer import load_actor_aliases  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402

# main._CYBER_CATEGORIES と一致させる (R-A ゲートの判定基準)。
_CYBER_CATEGORIES = frozenset(
    {
        "apt",
        "apt_leak",
        "malware",
        "incident",
        "incident_breach",
        "breach",
        "advisory",
        "vulnerability",
        "phishing",
    }
)
_ALLIED_NATIONS = frozenset({"us", "gb", "il", "fr", "de", "au", "ca", "nz", "kr", "in"})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="実際に article_entities へ追記")
    ap.add_argument("--ids", default="", help="対象 actor id (カンマ区切り)。未指定=同盟 nation")
    args = ap.parse_args()

    reg = load_actor_aliases()
    if args.ids.strip():
        target_ids = {x.strip() for x in args.ids.split(",") if x.strip()}
    else:
        target_ids = {a.id for a in reg.actors if (a.nation or "").lower() in _ALLIED_NATIONS}
    kind_of = {a.id: getattr(a, "kind", "group") for a in reg.actors}
    print(f"対象アクター {len(target_ids)} 件")

    repo = RunHistoryRepository()
    with repo._connect() as con:
        rows = con.execute(
            "SELECT article_id, category, "
            "COALESCE(title,'') || ' ' || COALESCE(summary,'') || ' ' || COALESCE(body,'') AS txt "
            "FROM articles WHERE status='posted'"
        ).fetchall()

    print(f"scan {len(rows)} posted articles ...")
    added: Counter[str] = Counter()
    skipped_ra: Counter[str] = Counter()
    pairs: set[tuple[str, str]] = set()
    for row in rows:
        aid, cat, txt = row[0], (row[1] or "").strip().lower(), row[2] or ""
        if not txt.strip():
            continue
        is_cyber = cat in _CYBER_CATEGORIES
        for actor in reg.find_all(txt):
            if actor.id not in target_ids:
                continue
            if kind_of.get(actor.id, "group") != "group" and not is_cyber:
                skipped_ra[actor.id] += 1  # organization は cyber 記事のみ (R-A)
                continue
            if (aid, actor.id) not in pairs:
                pairs.add((aid, actor.id))
                added[actor.id] += 1

    print(f"\n検出 (R-A 通過): {len(pairs)} (article, actor) ペア")
    for k, v in added.most_common():
        print(f"  {k}: {v}")
    if skipped_ra:
        print("R-A で除外 (organization × 非cyber 言及):")
        for k, v in skipped_ra.most_common():
            print(f"  {k}: {v}")

    if not args.apply:
        print("\n[dry-run] --apply で article_entities へ追記")
        return 0

    per_art: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for aid, actid in pairs:
        per_art[aid].append(("actor", actid))
    now = datetime.now(UTC)
    for aid, ents in per_art.items():
        repo.add_article_entities(aid, ents, when=now)
    print(f"\n[applied] {len(pairs)} ペアを追記")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
