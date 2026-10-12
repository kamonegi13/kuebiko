"""別名の同一視 (org identity) を群化に入れた効果を過去の記事に当てて測る (読み取り専用)。

``eval_entity_norm_replay`` の再現器を土台に、victim_org の結合キーを「同一と判定された名前の対」で
さらに畳む変種 (V1 = 現行の org_match_key / V3 = + 判定済みの別名) を比べる。
判定済みの対は ``--judged`` (jsonl: {"a","b","logit"}) で渡す。名前対の判定は別工程 (Jev / ML)。
使い方: python scripts/eval_org_identity_replay.py <articles.txt> <ents.txt> <alert_dups.jsonl> \
  <judged.jsonl> <logit_threshold> <out.json>
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import eval_entity_norm_replay as er  # noqa: E402

from src.cti.org_normalizer import org_match_key  # noqa: E402
from src.eventnews import pair_model  # noqa: E402


def build_alias_map(judged: list[dict[str, Any]], threshold: float) -> dict[str, str]:
    """logit >= threshold の名前対を union-find で連結し、key -> 代表 key の写像を返す。"""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for row in judged:
        if row["logit"] < threshold:
            continue
        ka, kb = org_match_key(row["a"]), org_match_key(row["b"])
        if ka and kb and ka != kb:
            parent[find(ka)] = find(kb)
    return {k: find(k) for k in list(parent)}


def main() -> None:
    art_path, ent_path, dups_path, judged_path, thr, out_path = sys.argv[1:7]
    judged = [json.loads(x) for x in Path(judged_path).read_text().splitlines()]
    alias = build_alias_map(judged, float(thr))
    sizes: dict[str, int] = defaultdict(int)
    for rep in alias.values():
        sizes[rep] += 1
    base_key = er._key

    def key_with_alias(kind: str, etype: str, value: str) -> tuple[str, str]:
        t, k = base_key("new", etype, value)
        return (t, alias.get(k, k)) if etype == "victim_org" and kind == "alias" else (t, k)

    er._key = key_with_alias
    arts, ents = er.load(art_path, ent_path)
    # 頻出ガードは**別名を畳む前**のキーで数える (本番は entity を作る時点でガードを適用し、
    # その後でキーを置き換える。畳んだ後の件数で数えると、別名が多い話題が丸ごとガードに掛かり
    # 本番では起きない辺の消失が再現側にだけ出る)。代表キーは、元のキーのどれかが
    # ガードを通れば通る (件数が最小の元キーで判定)。
    _, orig_times = er.build(arts, ents, er.Variant("orig", "new", False))
    members: dict[str, list[str]] = defaultdict(list)
    for key, rep in alias.items():
        members[rep].append(key)
    base_count = er.count_at

    def count_before_alias(times: dict[Any, Any], k: tuple[str, str], t: datetime) -> int:
        if k[0] != "victim_org" or k[1] not in members:
            return base_count(orig_times, k, t)
        return min(base_count(orig_times, (k[0], mk), t) for mk in members[k[1]])

    model = pair_model.load_model()
    assert model is not None
    cutoff = max(a["ts"] for a in arts.values()) - timedelta(days=er.DAYS_EVAL)
    dups = [json.loads(x) for x in Path(dups_path).read_text().splitlines()]
    target = [d for d in dups if d["class"] != "roundup_or_derived_not_same_event"]
    variants = [er.Variant("V1_orgnorm", "new", False), er.Variant("V3_alias", "alias", False)]
    approved = {variants[0].name: er.edges(arts, ents, variants[0], model, cutoff)}
    er.count_at = (
        count_before_alias  # 現行 (V1) は元の数え方、別名つき (V3) だけ畳む前の件数で数える
    )
    approved[variants[1].name] = er.edges(arts, ents, variants[1], model, cutoff)
    result: dict[str, object] = {
        "alias_pairs": sum(1 for r in judged if r["logit"] >= float(thr)),
        "alias_keys": len(alias),
        "max_alias_component": max(sizes.values(), default=0),
    }
    for name, ap in approved.items():
        comp = er.components(set(ap))
        rec = sum(
            1
            for d in target
            if comp.get(d["article"]) is not None and comp.get(d["article"]) == comp.get(d["prior"])
        )
        grp: dict[int, int] = defaultdict(int)
        for c in comp.values():
            grp[c] += 1
        result[name] = {
            "edges": len(ap),
            "groups_ge2": len(grp),
            "max_group": max(grp.values(), default=0),
            "alert_dup_recovered": rec,
            "alert_dup_total": len(target),
        }
    base = approved["V1_orgnorm"]
    result["new_edges"] = [
        {
            "a": min(p),
            "b": max(p),
            "ta": arts[min(p)]["title"],
            "tb": arts[max(p)]["title"],
            "proba": info["proba"],
            "cos": info["cos"],
            "via": info["via"],
        }
        for p, info in approved["V3_alias"].items()
        if p not in base
    ]
    result["lost_edges"] = [
        {
            "a": min(p),
            "b": max(p),
            "ta": arts[min(p)]["title"],
            "tb": arts[max(p)]["title"],
            "proba": info["proba"],
            "cos": info["cos"],
            "via": info["via"],
        }
        for p, info in base.items()
        if p not in approved["V3_alias"]
    ]
    Path(out_path).write_text(json.dumps(result, ensure_ascii=False, indent=1))
    summary = {k: v for k, v in result.items() if k not in ("new_edges", "lost_edges")}
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print("new edges", len(result["new_edges"]), "lost edges", len(result["lost_edges"]))  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
