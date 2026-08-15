"""Actor 辞書を MITRE ATT&CK の一次データで充実させる (Stage 2、一括 enrich 用)。

**記憶から書かず、MITRE ATT&CK STIX を実取得**して factual な field を充てる。
- 既存 actor: 空き field のみ enrich (curated な nation/description/family は上書きしない)。
- 新規 actor: ミッション該当 (国家系 CN/RU/KP/IR + 主要犯罪/ランサム) のみ additive 追加。

取得・パース・nation 推定ロジックは ``src/cti/mitre_sync.py`` と共通 (Stage 4 で集約)。
週次の逐次同期は ``mitre-actor-sync`` pipeline が担うため、本スクリプトは初期投入や
全量再 enrich のときのみ使う。

使い方:
    uv run python scripts/enrich_actors_from_mitre.py            # review 出力 (.enriched.yaml)
    uv run python scripts/enrich_actors_from_mitre.py --apply    # config/cti/actor_aliases.yaml に適用
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.cti.actor_editor import load_actors_raw, render_actors_yaml  # noqa: E402
from src.cti.mitre_sync import (  # noqa: E402
    MitreGroup,
    fetch_mitre_groups,
    slugify_actor_id,
)


def _index_existing(actors: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """既存 actor を mitre_group / 名前 (lower) で逆引きする index。"""
    idx: dict[str, dict[str, Any]] = {}
    for a in actors:
        if not isinstance(a, dict):
            continue
        if a.get("mitre_group"):
            idx[str(a["mitre_group"]).upper()] = a
        for name in [a.get("canonical"), *(a.get("aliases") or [])]:
            if name:
                idx.setdefault(str(name).lower(), a)
    return idx


def _enrich_existing(actor: dict[str, Any], grp: MitreGroup) -> int:
    """既存 actor の **空き field のみ** MITRE で埋める。変更数を返す (curated は保護)。"""
    changed = 0
    if not actor.get("mitre_group") and grp.mitre_id:
        actor["mitre_group"] = grp.mitre_id
        changed += 1
    if not actor.get("summary") and grp.summary:
        actor["summary"] = grp.summary
        changed += 1
    if not actor.get("associated_malware") and grp.associated_malware:
        actor["associated_malware"] = list(grp.associated_malware)
        changed += 1
    if not actor.get("references") and grp.references:
        actor["references"] = list(grp.references)
        changed += 1
    if not actor.get("mitre_ttps") and grp.ttps:
        actor["mitre_ttps"] = list(grp.ttps)
        changed += 1
    # aliases は union (既存を残しつつ MITRE 別名を追加)
    have = {x.lower() for x in (actor.get("aliases") or [])}
    have.add(str(actor.get("canonical", "")).lower())
    add = [a for a in grp.aliases if a.lower() not in have]
    if add:
        actor["aliases"] = [*(actor.get("aliases") or []), *add]
        changed += 1
    return changed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="config/cti/actor_aliases.yaml に直接書き込む")
    args = ap.parse_args()

    data = load_actors_raw()
    actors: list[dict[str, Any]] = data["actors"]
    idx = _index_existing(actors)
    print("fetching MITRE ATT&CK …", file=sys.stderr)
    groups = asyncio.run(fetch_mitre_groups())
    print(f"  {len(groups)} groups parsed", file=sys.stderr)

    enriched_n = 0
    added: list[dict[str, Any]] = []
    used_ids = {str(a.get("id")) for a in actors if isinstance(a, dict)}
    for grp in groups:
        match = idx.get(grp.mitre_id.upper()) or idx.get(grp.canonical.lower())
        if not match:
            match = next((idx[a.lower()] for a in grp.aliases if a.lower() in idx), None)
        if match:
            if _enrich_existing(match, grp):
                enriched_n += 1
            continue
        # 新規: ミッション該当 (国家系 or 犯罪) のみ追加
        if not grp.nation and not grp.is_criminal:
            continue
        base = slugify_actor_id(grp.canonical)
        aid = base
        n = 2
        while aid in used_ids:
            aid = f"{base}_{n}"
            n += 1
        used_ids.add(aid)
        new_actor: dict[str, Any] = {
            "id": aid,
            "canonical": grp.canonical,
            "aliases": list(grp.aliases),
            "mitre_group": grp.mitre_id,
            "summary": grp.summary,
            "associated_malware": list(grp.associated_malware),
            "references": list(grp.references),
        }
        if grp.nation:
            new_actor["nation"] = grp.nation
        else:
            new_actor["motivation"] = "financial"  # 犯罪系
        added.append(new_actor)

    actors.extend(added)
    print(f"\n既存 enrich: {enriched_n} 件 / 新規追加: {len(added)} 件", file=sys.stderr)
    print("--- 新規追加 actor (要レビュー: nation 推定の妥当性) ---", file=sys.stderr)
    for a in added:
        tag = a.get("nation", "(犯罪)")
        print(f"  + {a['canonical']:28} {a['mitre_group']:6} nation={tag}", file=sys.stderr)

    content = render_actors_yaml(data)
    if args.apply:
        Path("config/cti/actor_aliases.yaml").write_text(content, encoding="utf-8")
        print("\n✅ config/cti/actor_aliases.yaml に適用しました", file=sys.stderr)
    else:
        out = Path("config/cti/actor_aliases.enriched.yaml")
        out.write_text(content, encoding="utf-8")
        print(
            f"\n📝 review 出力: {out} (確認後 --apply で適用、または diff で精査)", file=sys.stderr
        )


if __name__ == "__main__":
    main()
