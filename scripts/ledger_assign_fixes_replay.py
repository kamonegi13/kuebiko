"""台帳の上流の修正 3 旗の過去 30 日の再現 (読み取り専用)。
設計は docs/ledger_upstream_fixes_2026_11_17.md。

SELECT のみ。本番と同じ純粋関数 (clustering._strict_keeps / dup_rescue.decide_dup_rescue /
assign_strong_anchor.should_drop_assignment) を既存の行に当てて JSONL に出す。
ルールは出力に合わせて調整しない。

usage (使い捨てコンテナ。DATABASE_URL が compose 内でのみ解決するため):
  cd /Users/juzo1192/git/auto-post && docker compose run --rm --no-deps -T \
    -v <worktree>/src:/app/src:ro -v <worktree>/scripts:/app/scripts:ro \
    -v $PWD/data/eval:/app/data/eval kuebiko python scripts/ledger_assign_fixes_replay.py \
    --out data/eval/ledger_fix_1117


近似 (a): 最古の seed 行を seed、残りを拡張候補として扱い、題名を claim の代わりにする。
近似 (b): 候補の手がかり・根は新情勢の opened_at より前の根拠だけから導く。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.assessment.assign_strong_anchor import CYBER_DOMAINS, should_drop_assignment
from src.assessment.assignment import split_anchor_keys
from src.assessment.dup_rescue import (
    RescueCandidate,
    cluster_names,
    decide_dup_rescue,
    roundup_roots,
)
from src.assessment.name_relevance import article_names, is_relevant, matched_names
from src.assessment.situation_store import SituationStore
from src.synthesis.grounded.clustering import _strict_keeps, _tokens, anchor_entities

CHUNK = 400


def _rows(store: SituationStore, sql: str, params: list[Any]) -> list[Any]:
    with store._repo._connect() as conn:  # noqa: SLF001
        return list(conn.execute(sql, params).fetchall())


def _chunks(ids: list[str]) -> list[list[str]]:
    return [ids[i : i + CHUNK] for i in range(0, len(ids), CHUNK)]


def _entities(store: SituationStore, ids: list[str]) -> dict[str, set[str]]:
    return store._repo.entity_keys_for_articles(ids)  # noqa: SLF001


def _titles(store: SituationStore, ids: list[str]) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for ch in _chunks(ids):
        ph = ",".join("?" for _ in ch)
        for r in _rows(
            store,
            "SELECT article_id, MIN(title) AS t, MIN(category) AS c FROM articles"  # noqa: S608
            f" WHERE article_id IN ({ph}) GROUP BY article_id",
            ch,
        ):
            out[str(r["article_id"])] = (str(r["t"] or ""), str(r["c"] or ""))
    return out


def _kinds(store: SituationStore, ids: list[str]) -> dict[str, str]:
    return store._repo.get_article_kinds(ids)  # noqa: SLF001


def _roots(store: SituationStore, ids: list[str]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for ch in _chunks(ids):
        ph = ",".join("?" for _ in ch)
        for r in _rows(
            store,
            "SELECT m.article_id AS a, COALESCE(NULLIF(i.merged_into, ''), m.item_id) AS root"  # noqa: S608
            f" FROM event_item_members m JOIN event_items i ON i.id = m.item_id"
            f" WHERE m.article_id IN ({ph})",
            ch,
        ):
            out[str(r["a"])].add(str(r["root"]))
    return out


def _claims(store: SituationStore) -> dict[str, list[tuple[str, str]]]:
    """情勢 → [(created_at, claim)] (rev 昇順)。"""
    out: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for r in _rows(
        store,
        "SELECT situation_id, created_at, claim FROM situation_revisions"
        " ORDER BY situation_id, rev",
        [],
    ):
        out[str(r["situation_id"])].append((str(r["created_at"]), str(r["claim"] or "")))
    return out


def _claim_until(claims: list[tuple[str, str]], until: str | None = None) -> str:
    rows = [c for t, c in claims if until is None or t <= until]
    return rows[-1] if rows else ""


def replay_seed(
    store: SituationStore, cutoff: str, claims: dict[str, list[tuple[str, str]]]
) -> list[dict[str, Any]]:
    ev = _rows(
        store,
        "SELECT e.situation_id AS sid, e.article_id AS aid, e.added_at AS at, s.title AS st"
        " FROM situation_evidence e JOIN situations s ON s.situation_id = e.situation_id"
        " WHERE e.assigned_by = 'seed' AND e.weak_at IS NULL AND e.added_at >= ?"
        " ORDER BY e.situation_id, e.added_at, e.article_id",
        [cutoff],
    )
    by_sit: dict[str, list[Any]] = defaultdict(list)
    for r in ev:
        by_sit[str(r["sid"])].append(r)
    ids = sorted({str(r["aid"]) for r in ev})
    ents, tit, kinds = _entities(store, ids), _titles(store, ids), _kinds(store, ids)
    out: list[dict[str, Any]] = []
    for sid, rows in by_sit.items():
        seed_id = str(rows[0]["aid"])
        anchors = anchor_entities(ents, (seed_id,))
        claim_tok = _tokens(str(rows[0]["st"])) | _tokens(tit.get(seed_id, ("",))[0])
        for r in rows[1:]:
            aid = str(r["aid"])
            title, cat = tit.get(aid, ("", ""))
            ent_share = sum(1 for e in ents.get(aid, set()) if e in anchors)
            shared = _tokens(title) & claim_tok
            if not (ent_share >= 1 or len(shared) >= 2):
                continue  # 従来の規則でも拡張されない行は対象外
            art = {"title": title, "category": cat}
            sit_text = f"{rows[0]['st']} {_claim_until(claims.get(sid, []))}"
            if not _strict_keeps(art, aid, ent_share, shared, ents, kinds, sit_text):
                out.append(
                    {
                        "flag": "SEED_STRICT",
                        "situation_id": sid,
                        "situation_title": str(rows[0]["st"]),
                        "article_id": aid,
                        "article_title": title,
                        "reason": "roundup/多数の被害組織/一般語のみの重なり",
                        "shared_tokens": sorted(shared),
                    }
                )
    return out


def replay_dup_rescue(
    store: SituationStore, cutoff: str, claims: dict[str, list[tuple[str, str]]]
) -> list[dict[str, Any]]:
    sits = _rows(
        store,
        "SELECT situation_id, title, opened_at, closed_at FROM situations"
        " WHERE kind = 'event' AND opened_at >= ? ORDER BY opened_at",
        [cutoff],
    )
    pool = _rows(
        store,
        "SELECT situation_id, title, opened_at, closed_at FROM situations WHERE kind = 'event'",
        [],
    )
    ev = _rows(
        store,
        "SELECT situation_id AS sid, article_id AS aid, added_at AS at, assigned_by AS by,"
        " assessed_at AS ass, weak_at AS weak, polarity AS pol FROM situation_evidence",
        [],
    )
    ids = sorted({str(r["aid"]) for r in ev})
    ents, roots = _entities(store, ids), _roots(store, ids)
    tit = {a: t for a, (t, _) in _titles(store, ids).items()}
    bad_roots = roundup_roots(store, sorted({x for v in roots.values() for x in v}))
    ev_by: dict[str, list[Any]] = defaultdict(list)
    for r in ev:
        ev_by[str(r["sid"])].append(r)
    out: list[dict[str, Any]] = []
    for s in sits:
        sid, opened = str(s["situation_id"]), str(s["opened_at"])
        seeds = [str(r["aid"]) for r in ev_by[sid] if str(r["at"]) <= opened and r["by"] == "seed"]
        if not seeds:
            seeds = [str(r["aid"]) for r in ev_by[sid] if str(r["at"]) <= opened]
        names = cluster_names(seeds, ents, tit)
        c_roots = {x for a in seeds for x in roots.get(a, set())} - bad_roots
        cands = []
        for o in pool:
            oid = str(o["situation_id"])
            if oid == sid or str(o["opened_at"]) >= opened:
                continue
            if o["closed_at"] and str(o["closed_at"]) <= opened:
                continue
            text = f"{o['title']} {_claim_until(claims.get(oid, []), opened)}"
            if len(matched_names(names, text)) < 2:
                continue
            rts = {
                x
                for r in ev_by[oid]
                if str(r["at"]) < opened and r["ass"] and not r["weak"] and r["pol"] == "supports"
                for x in roots.get(str(r["aid"]), set())
            } - bad_roots
            cands.append(RescueCandidate(oid, text, frozenset(rts)))
        v = decide_dup_rescue(names, c_roots, cands)
        if v is not None:
            tgt = next(o for o in pool if str(o["situation_id"]) == v.situation_id)
            out.append(
                {
                    "flag": "DUP_RESCUE",
                    "new_situation_id": sid,
                    "new_title": str(s["title"]),
                    "new_article_ids": seeds,
                    "target_situation_id": v.situation_id,
                    "target_title": str(tgt["title"]),
                    "shared_names": list(v.shared_names),
                    "shared_roots": list(v.shared_roots),
                }
            )
    return out


def replay_assign(
    store: SituationStore, cutoff: str, claims: dict[str, list[tuple[str, str]]]
) -> tuple[list[dict[str, Any]], Counter[str]]:
    rows = _rows(
        store,
        "SELECT e.situation_id AS sid, e.article_id AS aid, e.assigned_by AS rule,"
        " s.title AS st, s.domain AS dom, s.anchors AS anc"
        " FROM situation_evidence e JOIN situations s ON s.situation_id = e.situation_id"
        " WHERE e.assigned_by IN ('nation', 'token') AND e.weak_at IS NULL AND e.added_at >= ?",
        [cutoff],
    )
    ids = sorted({str(r["aid"]) for r in rows})
    ents, tit = _entities(store, ids), _titles(store, ids)
    out: list[dict[str, Any]] = []
    stats: Counter[str] = Counter()
    for r in rows:
        dom = str(r["dom"])
        anc = frozenset(json.loads(r["anc"])) if r["anc"] else frozenset()
        shared = (
            split_anchor_keys(frozenset(ents.get(str(r["aid"]), set())))[0]
            & (split_anchor_keys(anc)[0])
        )
        rule = str(r["rule"])
        drop = should_drop_assignment(rule=rule, domain=dom, shared_strong=shared)
        if drop:  # 固有の名前が状況の題・最新の判定文に出れば関係あり (本番と同じ規則)
            names = article_names(
                tit.get(str(r["aid"]), ("", ""))[0], ents.get(str(r["aid"]), set())
            )
            sit_text = f"{r['st']} {_claim_until(claims.get(str(r['sid']), []))}"
            drop = not is_relevant(names, sit_text)
        stats[f"total:{dom}"] += 1
        if drop:
            stats[f"drop:{dom}"] += 1
            out.append(
                {
                    "flag": "ASSIGN_STRONG_ANCHOR",
                    "situation_id": str(r["sid"]),
                    "situation_title": str(r["st"]),
                    "domain": dom,
                    "article_id": str(r["aid"]),
                    "article_title": tit.get(str(r["aid"]), ("", ""))[0],
                    "rule": str(r["rule"]),
                }
            )
    return out, stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="data/eval/ledger_fix_1117/round2")
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()
    cutoff = (datetime.now(UTC) - timedelta(days=args.days)).isoformat()
    store = SituationStore()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    claims = _claims(store)
    assign, stats = replay_assign(store, cutoff, claims)
    results = {
        "seed_strict": replay_seed(store, cutoff, claims),
        "dup_rescue": replay_dup_rescue(store, cutoff, claims),
        "assign_strong_anchor": assign,
    }
    for name, rows in results.items():
        with (out_dir / f"{name}.jsonl").open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"{name}: {len(rows)} rows")
    non_cyber_dropped = sum(
        v for k, v in stats.items() if k.startswith("drop:") and k[5:] not in CYBER_DOMAINS
    )
    print("assign by domain:", dict(sorted(stats.items())))
    print("non-cyber domains dropped (must be 0):", non_cyber_dropped)


if __name__ == "__main__":
    main()
