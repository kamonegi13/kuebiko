"""組織名の正規化・頻出ガード緩和の効果を、過去の記事に当てて測る (読み取り専用の再現)。

本番 DB には書かない。入力は ``docker exec postgres psql`` で書き出したファイル
(記事・埋込・entity)。ML の判定は pair_model を LLM 欠測の閾値で回した近似
(要約埋込・種別は使わない) — 本番の LLM 併用の判定とは一致しないので、
**変種どうしの差**を見る道具で、絶対値は参考。
使い方: python scripts/eval_entity_norm_replay.py \
  <articles.txt> <ents.txt> <alert_dups.jsonl> <out.json>
"""

from __future__ import annotations

import bisect
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.assessment.evidence_verify import normalize_for_match
from src.cti.org_normalizer import org_match_key
from src.eventnews import pair_model
from src.eventnews.models import ENTITY_FREQ_CAP, FREQ_CAP_EXEMPT_TYPES, WINDOW_HOURS
from src.eventnews.pair_features import PairSide, concrete_shared_names, pair_features

SEP = "\x1f"
DAYS_EVAL = 30
COUNT_DAYS = 14
RESCUE_COS = 0.80


def _key(kind: str, etype: str, value: str) -> tuple[str, str]:
    if etype != "victim_org":
        return (etype, value)
    return (etype, org_match_key(value) if kind != "old" else normalize_for_match(value))


def load(art_path: str, ent_path: str) -> tuple[dict[str, dict], dict[str, list[tuple[str, str]]]]:
    arts: dict[str, dict] = {}
    for line in Path(art_path).read_text(encoding="utf-8").splitlines():
        p = line.split(SEP)
        if len(p) != 8:
            continue
        aid, ts, imp, cat, feed, title, vhex, dim = p
        vec = np.frombuffer(bytes.fromhex(vhex), dtype=np.float32)
        if vec.shape[0] != int(dim) or not np.linalg.norm(vec):
            continue
        arts[aid] = {
            "ts": datetime.fromisoformat(ts),
            "cat": cat,
            "feed": feed,
            "title": title,
            "vec": vec / np.linalg.norm(vec),
        }
    ents: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for line in Path(ent_path).read_text(encoding="utf-8").splitlines():
        p = line.split(SEP)
        if len(p) == 3 and p[0] in arts:
            ents[p[0]].append((p[1], p[2]))
    return arts, ents


class Variant:
    def __init__(self, name: str, key_kind: str, rescue: bool) -> None:
        self.name, self.key_kind, self.rescue = name, key_kind, rescue


def build(arts: dict, ents: dict, v: Variant):
    """variant ごとに (記事 → 全キー) と (キー → 出現時刻の昇順) を作る。"""
    allkeys: dict[str, set[tuple[str, str]]] = {}
    times: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    for aid, rows in ents.items():
        ks = {_key(v.key_kind, t, val) for t, val in rows}
        allkeys[aid] = ks
        for k in ks:
            times[k].append(arts[aid]["ts"])
    for k in times:
        times[k].sort()
    return allkeys, times


def count_at(times: dict, k: tuple[str, str], t: datetime) -> int:
    ts = times[k]
    return bisect.bisect_right(ts, t) - bisect.bisect_right(ts, t - timedelta(days=COUNT_DAYS))


def edges(arts: dict, ents: dict, v: Variant, model, cutoff: datetime) -> dict[frozenset, dict]:
    allkeys, times = build(arts, ents, v)
    by_key: dict[tuple[str, str], list[str]] = defaultdict(list)
    for aid, ks in allkeys.items():
        for k in ks:
            by_key[k].append(aid)
    out: dict[frozenset, dict] = {}
    for k, aids in by_key.items():
        if len(aids) > 400:
            continue
        aids = sorted(aids, key=lambda a: arts[a]["ts"])
        for i, a in enumerate(aids):
            for b in aids[i + 1 :]:
                tb = arts[b]["ts"]
                if tb < cutoff:
                    continue
                if (tb - arts[a]["ts"]).total_seconds() > WINDOW_HOURS * 3600:
                    break
                pair = frozenset((a, b))
                if pair in out:
                    continue
                capped = (
                    k[0] not in FREQ_CAP_EXEMPT_TYPES and count_at(times, k, tb) > ENTITY_FREQ_CAP
                )
                cos = float(np.dot(arts[a]["vec"], arts[b]["vec"]))
                if capped and not (v.rescue and k[0] == "victim_org" and cos >= RESCUE_COS):
                    continue
                out[pair] = {"via": k, "capped": capped}
    # ML で採点 (選別: 非 actor の具体名を共有 = select_pairs と同じ条件)
    approved: dict[frozenset, dict] = {}
    for pair, info in out.items():
        a, b = tuple(pair)
        ea = frozenset(x for x in allkeys[a] if x[0] != "x")
        eb = frozenset(x for x in allkeys[b] if x[0] != "x")
        ea = frozenset(x for x in ea if _kept(x, times, arts[a]["ts"], v))
        eb = frozenset(x for x in eb if _kept(x, times, arts[b]["ts"], v))
        la = PairSide(
            a, arts[a]["title"], arts[a]["cat"], arts[a]["feed"], arts[a]["ts"], ea, arts[a]["vec"]
        )
        lb = PairSide(
            b, arts[b]["title"], arts[b]["cat"], arts[b]["feed"], arts[b]["ts"], eb, arts[b]["vec"]
        )
        if not concrete_shared_names(la, lb):
            continue
        feats = pair_features(la, lb)
        proba = model.probability(feats + [0.0, 0.0])
        if proba >= model.threshold_llm_off:
            approved[pair] = {**info, "proba": round(float(proba), 3), "cos": round(feats[0], 3)}
    return approved


def _kept(k, times, t, v) -> bool:
    if k[0] in FREQ_CAP_EXEMPT_TYPES:
        return True
    if count_at(times, k, t) <= ENTITY_FREQ_CAP:
        return True
    return v.rescue and k[0] == "victim_org"


def components(pairs: set[frozenset]) -> dict[str, int]:
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for p in pairs:
        a, b = tuple(p)
        parent[find(a)] = find(b)
    return {x: hash(find(x)) for x in list(parent)}


def main() -> None:
    art_path, ent_path, dups_path, out_path = sys.argv[1:5]
    arts, ents = load(art_path, ent_path)
    model = pair_model.load_model()
    assert model is not None
    cutoff = max(a["ts"] for a in arts.values()) - timedelta(days=DAYS_EVAL)
    variants = [
        Variant("V0_base", "old", False),
        Variant("V1_orgnorm", "new", False),
        Variant("V2_orgnorm_rescue", "new", True),
    ]
    dups = [json.loads(line) for line in Path(dups_path).read_text().splitlines()]
    target = [d for d in dups if d["class"] != "roundup_or_derived_not_same_event"]
    result: dict[str, object] = {"articles": len(arts), "cutoff": cutoff.isoformat()}
    approved_by: dict[str, dict[frozenset, dict]] = {}
    for v in variants:
        ap = edges(arts, ents, v, model, cutoff)
        approved_by[v.name] = ap
        comp = components(set(ap))
        sizes: dict[int, int] = defaultdict(int)
        for c in comp.values():
            sizes[c] += 1
        rec = sum(
            1
            for d in target
            if comp.get(d["article"]) is not None and comp.get(d["article"]) == comp.get(d["prior"])
        )
        result[v.name] = {
            "edges": len(ap),
            "groups_ge2": len(sizes),
            "articles_in_groups": len(comp),
            "max_group": max(sizes.values(), default=0),
            "alert_dup_recovered": rec,
            "alert_dup_total": len(target),
        }
    base = approved_by["V0_base"]
    new_edges = []
    for name in ("V1_orgnorm", "V2_orgnorm_rescue"):
        prev = base if name == "V1_orgnorm" else approved_by["V1_orgnorm"]
        for pair, info in approved_by[name].items():
            if pair not in prev:
                a, b = sorted(pair)
                new_edges.append(
                    {
                        "variant": name,
                        "a": a,
                        "b": b,
                        "ta": arts[a]["title"],
                        "tb": arts[b]["title"],
                        "via": info["via"],
                        "capped": info["capped"],
                        "proba": info["proba"],
                        "cos": info["cos"],
                    }
                )
    result["new_edges"] = new_edges
    Path(out_path).write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "new_edges"}, ensure_ascii=False, indent=1
        )
    )
    print("new edges", len(new_edges))


if __name__ == "__main__":
    main()
