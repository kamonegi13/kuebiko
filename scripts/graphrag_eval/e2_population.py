"""E2: 線の母集団を導き、除外して標本を抜き、Opus に見せる事象の材料を書く (読むだけ)。

使い捨てのコンテナで read-only のセッションで動かす (prereg graphrag_e2_edge_precision.md §2・§4):

    docker compose run --rm --no-deps -T \
      -e PGOPTIONS='-c default_transaction_read_only=on' \
      -v "$PWD/scripts/graphrag_eval:/app/scripts/graphrag_eval:ro" \
      kuebiko python -m scripts.graphrag_eval.e2_population --out data/eval/graphrag_e2_2026_11_02 [--count-only]

DB へは書かない (ensure_pg_schema を無効にし、セッションも read-only)。Opus は呼ばない。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.graphrag_eval.sampling import (
    choose_sample,
    exclude_pairs,
    pair_key,
    pick_members,
    resolve_root,
    supplement_pool,
)

SEED = 20261102
N_PER_TYPE = 60
SUPPLEMENT_MAX = 17  # prereg §9 (2026-11-02): 元の数えで除外 4 に当たった incident の数
WINDOW_DAYS = 60  # 本番の relations.WINDOW_DAYS と同じ
MAX_MEMBERS = 6
SUMMARY_MAX = 600
_CHUNK = 500

PRIOR_SAMPLE_FILES = (
    "data/eval/graphrag_line_samples_v1.jsonl",
    "data/eval/graphrag_line_samples_v2.jsonl",
    "data/eval/graphrag_line_audit_v1.jsonl",
    "data/eval/graphrag_line_audit_v2.jsonl",
    "data/eval/graphrag_same_target_v2_all.json",
    "data/eval/graphrag_same_target_v2_sample.json",
)
RELATION_GOLD_FILES = (
    "data/mlx/event_relations_goldset.jsonl",
    "data/mlx/event_relations_goldset_pool.jsonl",
    "data/mlx/event_relations_goldset_r2.jsonl",
    "data/mlx/event_relations_goldset_v2.jsonl",
    "data/mlx/event_relations_goldset_v3.jsonl",
)
GUARD_FILES = (
    ("data/mlx/n20_teacher/guard_n20_v2.json", "ids"),
    ("data/mlx/s26_teacher/guard_s26.json", "article_ids"),
)


def _rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix == ".json":
        data = json.loads(path.read_text("utf-8"))
        return list(data) if isinstance(data, list) else []
    return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]


def prior_pairs_and_events(root: Path) -> tuple[frozenset[str], frozenset[str]]:
    pairs: set[str] = set()
    for rel in PRIOR_SAMPLE_FILES + RELATION_GOLD_FILES:
        for r in _rows(root / rel):
            if "a_id" in r and "b_id" in r:
                pairs.add(pair_key(str(r["a_id"]), str(r["b_id"])))
            elif "key" in r and "|" in str(r["key"]):
                a, b = str(r["key"]).split("|", 1)
                pairs.add(pair_key(a, b))
    events = {e for k in pairs for e in k.split("|")}
    return frozenset(pairs), frozenset(events)


def guard_article_ids(root: Path) -> frozenset[str]:
    out: set[str] = set()
    for rel, field in GUARD_FILES:
        out |= {str(x) for x in json.loads((root / rel).read_text("utf-8"))[field]}
    return frozenset(out)


def _repo() -> Any:
    import src.storage.db_backend as backend

    backend.ensure_pg_schema = lambda *a, **k: None  # 読むだけ
    from src.storage.run_history import RunHistoryRepository

    return RunHistoryRepository()


def derive_populations(repo: Any) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]]]:
    """組の鍵 → 線の記録 (Opus には見せない)、型 → 組の鍵。"""
    from src.eventnews.relation_features import actor_helpers, load_event_features
    from src.eventnews.relation_model import load_relation_model
    from src.eventnews.relation_pair_features import load_centroids
    from src.eventnews.relations import (
        INCIDENT_TYPES,
        derive_relations,
        derive_with_model,
        index_relations,
    )

    nation_of, related = actor_helpers()
    events = load_event_features(repo, days=WINDOW_DAYS)
    centroids = load_centroids(repo, events)
    rels = derive_with_model(
        events,
        model=load_relation_model(),
        centroids=centroids,
        nation_of=nation_of,
        related_actors=related,
    )
    shown = {id(r) for rs in index_relations(rels).values() for r in rs}
    info: dict[str, dict[str, Any]] = {}
    pops: dict[str, set[str]] = {"same_actor": set(), "incident": set(), "lineage": set()}
    for r in rels:
        if id(r) not in shown:
            continue
        k = pair_key(r.a, r.b)
        t = "same_actor" if r.rel_type == "same_actor" else "incident"
        if r.rel_type != "same_actor" and r.rel_type not in INCIDENT_TYPES | {"incident"}:
            continue
        pops[t].add(k)
        info.setdefault(k, {}).setdefault("edges", []).append(
            {"type": t, "rel_type": r.rel_type, "basis": list(r.basis), **dict(r.extra)}
        )
    for r in derive_relations(events, nation_of=nation_of, related_actors=related):
        if r.rel_type == "side":
            k = pair_key(r.a, r.b)
            pops["lineage"].add(k)
            info.setdefault(k, {}).setdefault("edges", []).append(
                {"type": "lineage", "rel_type": "side_rule", "basis": list(r.basis)}
            )
    info["__meta__"] = {"events": len(events), "relations": len(rels)}
    return info, pops


def focus_lineage_pairs(repo: Any) -> list[str]:
    with repo._connect() as conn:  # noqa: SLF001 — 読み取り専用
        rows = conn.execute(
            "SELECT item_a, item_b FROM event_merge_vetoes WHERE klass = 'lineage'"
        ).fetchall()
    return sorted({pair_key(str(r["item_a"]), str(r["item_b"])) for r in rows})


def merged_map(repo: Any) -> dict[str, str | None]:
    with repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute("SELECT id, merged_into FROM event_items").fetchall()
    return {str(r["id"]): (str(r["merged_into"]) if r["merged_into"] else None) for r in rows}


def guarded_events(repo: Any, guard: frozenset[str]) -> frozenset[str]:
    ids = sorted(guard)
    out: set[str] = set()
    with repo._connect() as conn:  # noqa: SLF001
        for i in range(0, len(ids), _CHUNK):
            chunk = ids[i : i + _CHUNK]
            ph = ",".join("?" for _ in chunk)
            out |= {
                str(r["item_id"])
                for r in conn.execute(
                    f"SELECT DISTINCT item_id FROM event_item_members WHERE article_id IN ({ph})",  # noqa: S608
                    chunk,
                ).fetchall()
            }
    return frozenset(out)


def event_payload(repo: Any, item_id: str) -> dict[str, Any]:
    """Opus に見せる 1 事象の材料 (見出し・期間・構成記事の題・媒体・要約・主題アクターの札)。"""
    from src.assessment.situation_track import registry_is_group
    from src.eventnews.relation_features import _trusted_subjects

    is_group = registry_is_group()
    with repo._connect() as conn:  # noqa: SLF001
        item = conn.execute(
            "SELECT first_reported_at, last_reported_at FROM event_items WHERE id = ?", (item_id,)
        ).fetchone()
        mem = conn.execute(
            "SELECT m.article_id, MIN(a.title) AS title, MIN(a.feed_title) AS feed_title,"
            " MIN(a.published_at) AS published_at, MIN(a.summary) AS summary,"
            " MIN(a.subject_actor_ids) AS subject_actor_ids,"
            " MIN(a.subject_actor_source) AS subject_actor_source,"
            " MIN(a.subject_actor_confidence) AS subject_actor_confidence,"
            " MIN(m.joined_at) AS joined_at"
            " FROM event_item_members m JOIN articles a ON a.article_id = m.article_id"
            " WHERE m.item_id = ? GROUP BY m.article_id ORDER BY MIN(m.joined_at), m.article_id",
            (item_id,),
        ).fetchall()
    versions = repo.latest_event_versions([item_id])
    rows = [dict(r) for r in mem]
    by_id = {str(r["article_id"]): r for r in rows}
    picked = pick_members([str(r["article_id"]) for r in rows], MAX_MEMBERS)
    tags: set[str] = set()
    for r in rows:
        tags |= _trusted_subjects(r, is_group)
    ver = versions.get(item_id)
    return {
        "event_id": item_id,
        "headline": ver.headline if ver is not None else str(rows[0]["title"] if rows else ""),
        "first_reported": str(item["first_reported_at"] if item else "")[:10],
        "last_reported": str(item["last_reported_at"] if item else "")[:10],
        "n_articles": len(rows),
        "subject_actor_tags": sorted(tags),
        "articles": [
            {
                "title": str(by_id[a]["title"] or ""),
                "source": str(by_id[a]["feed_title"] or ""),
                "published": str(by_id[a]["published_at"] or "")[:10],
                "summary": str(by_id[a]["summary"] or "")[:SUMMARY_MAX],
            }
            for a in picked
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--root", type=Path, default=Path("."))
    ap.add_argument("--count-only", action="store_true")
    ap.add_argument(
        "--supplement-incident",
        type=Path,
        help="元の sample.jsonl。incident だけ保護の記事の除外を外し、元に無い組を最大 17 組 (prereg §9 2026-11-02)",
    )
    args = ap.parse_args()
    repo = _repo()
    info, pops = derive_populations(repo)
    meta = info.pop("__meta__")
    focus = focus_lineage_pairs(repo)
    for k in focus:
        pops["lineage"].add(k)
        info.setdefault(k, {}).setdefault("edges", []).append(
            {"type": "lineage", "rel_type": "focus_lineage"}
        )
    prior_pairs, prior_events = prior_pairs_and_events(args.root)
    merged = merged_map(repo)
    roots = {e: resolve_root(e, merged) for e in merged}
    prior_roots = frozenset(roots.get(e, e) for e in prior_events)
    guard_ev = guarded_events(repo, guard_article_ids(args.root))
    excl = {
        t: exclude_pairs(
            keys,
            excluded_pairs=prior_pairs,
            excluded_events=prior_events,
            excluded_roots=prior_roots,
            root_of=roots,
            guarded_events=guard_ev,
        )
        for t, keys in pops.items()
    }
    counts: dict[str, Any] = {
        "derive": meta,
        "population": {t: len(v) for t, v in pops.items()},
        "focus_lineage_records": len(focus),
        "eligible": {t: len(e.kept) for t, e in excl.items()},
        "dropped": {t: e.dropped for t, e in excl.items()},
        "guarded_events": len(guard_ev),
        "prior_pairs": len(prior_pairs),
        "prior_events": len(prior_events),
        "exclusion_files_sha256": {
            rel: hashlib.sha256((args.root / rel).read_bytes()).hexdigest()
            for rel in (*PRIOR_SAMPLE_FILES, *RELATION_GOLD_FILES, *(g for g, _ in GUARD_FILES))
        },
        "relation_model_sha256": hashlib.sha256(
            (args.root / "config/models/relation_model.json").read_bytes()
        ).hexdigest(),
    }
    print(
        json.dumps(
            {k: v for k, v in counts.items() if k != "exclusion_files_sha256"},
            ensure_ascii=False,
            indent=1,
        )
    )
    if args.count_only:
        return 0
    if args.supplement_incident is not None:
        relaxed = exclude_pairs(
            pops["incident"],
            excluded_pairs=prior_pairs,
            excluded_events=prior_events,
            excluded_roots=prior_roots,
            root_of=roots,
            guarded_events=frozenset(),
        )
        original = {
            json.loads(x)["pair_key"]
            for x in args.supplement_incident.read_text("utf-8").splitlines()
            if x.strip()
        }
        pool = supplement_pool(relaxed.kept, original)
        sample = {"incident": choose_sample(pool, SUPPLEMENT_MAX, SEED)}
        counts["supplement"] = {
            "relaxed_eligible": len(relaxed.kept),
            "relaxed_dropped": relaxed.dropped,
            "original_incident_still_present": len(original & set(relaxed.kept)),
            "pool": len(pool),
            "max": SUPPLEMENT_MAX,
        }
    else:
        sample = {
            "same_actor": choose_sample(excl["same_actor"].kept, N_PER_TYPE, SEED),
            "incident": choose_sample(excl["incident"].kept, N_PER_TYPE, SEED),
            "lineage": choose_sample(
                excl["lineage"].kept, N_PER_TYPE, SEED, must_include=frozenset(focus)
            ),
        }
    chosen = sorted({k for v in sample.values() for k in v})
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / "sample.jsonl").open("w", encoding="utf-8") as fh:
        for k in chosen:
            a, b = k.split("|")
            row = {
                "pair_key": k,
                "types": sorted(t for t, keys in pops.items() if k in keys),
                "sampled_as": sorted(t for t, v in sample.items() if k in v),
                "edges": info.get(k, {}).get("edges", []),
                "a": event_payload(repo, a),
                "b": event_payload(repo, b),
            }
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    counts["sampled"] = {t: len(v) for t, v in sample.items()}
    counts["unique_pairs"] = len(chosen)
    counts["seed"] = SEED
    counts["created_at"] = datetime.now(UTC).isoformat()
    (args.out / "population.json").write_text(
        json.dumps(counts, ensure_ascii=False, indent=1) + "\n", "utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
