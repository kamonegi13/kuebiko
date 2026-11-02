"""E6: 整合の点検の候補 (C1〜C6・C10・対照) の母集団を数え、除外して標本を抜く (読むだけ、Opus なし)。

prereg docs/replay_prereg/graph_consistency_e6.md。read-only のセッションで動かす:

    DATABASE_URL=postgresql://kuebiko:<pw>@127.0.0.1:5433/kuebiko \
      PGOPTIONS='-c default_transaction_read_only=on' \
      uv run python -m scripts.graphrag_eval.e6_sample [--count-only]

prereg が HEAD の祖先で、このスクリプトが未コミット変更でないときだけ動く (後出し防止)。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import Any

from scripts.graphrag_eval.common import ROOT, assert_clean, now_iso, require_prereg, write_json
from scripts.graphrag_eval.e2_population import (
    GUARD_FILES,
    event_payload,
    guard_article_ids,
    guarded_events,
)
from scripts.graphrag_eval.e6_logic import (
    LAYERS,
    SEED,
    UNIT_TYPES,
    WINDOW_DAYS,
    Unit,
    control_pairs,
    exclude_units,
    in_window,
    is_c1,
    is_c2,
    is_c3,
    is_c4,
    is_c5,
    layer_summary,
    overlap_keys,
    sample_units,
    window_start,
)
from scripts.graphrag_eval.sampling import pair_key

PREREG = "graph_consistency_e6"
OUT_DIR = ROOT / "data" / "eval" / "graph_e6_2026_11"
E2_DIR = "data/eval/graphrag_e2_2026_11_02"
FEATURE_DAYS = 60  # 本番の relations と同じ窓で特徴を読み、窓 (14 日) は事象時刻で切り直す
SUMMARY_MAX = 600
_CHUNK = 500
GUARDED_FILES = (
    "scripts/graphrag_eval/e6_logic.py",
    "scripts/graphrag_eval/sampling.py",
    "scripts/graphrag_eval/common.py",
    "scripts/graphrag_eval/e2_population.py",
)


def _repo() -> Any:
    import src.storage.db_backend as backend

    backend.ensure_pg_schema = lambda *a, **k: None  # 読むだけ
    from src.storage.run_history import RunHistoryRepository

    return RunHistoryRepository()


def e2_exclusions(root: Path) -> tuple[frozenset[str], frozenset[str], dict[str, str]]:
    """E2 の組・事象と、読んだファイルの sha256。"""
    base = root / E2_DIR
    files = sorted(p for p in base.rglob("*.jsonl") if p.name in ("sample.jsonl", "gold.jsonl"))
    pairs: set[str] = set()
    shas: dict[str, str] = {}
    for p in files:
        shas[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
        for line in p.read_text("utf-8").splitlines():
            if line.strip():
                pairs.add(str(json.loads(line)["pair_key"]))
    events = frozenset(e for k in pairs for e in k.split("|"))
    return frozenset(pairs), events, shas


def event_times(repo: Any) -> dict[str, datetime]:
    """事象 id → 構成記事の事象時刻 (EVENT_TS_EXPR) の最大。統合済みの事象も含む。"""
    from src.storage.event_time import DEDUP_ARTICLES, EVENT_TS_EXPR

    sql = (
        f"SELECT m.item_id AS item_id, MAX({EVENT_TS_EXPR.format(a='a')}) AS ts"  # noqa: S608
        f" FROM event_item_members m JOIN {DEDUP_ARTICLES} a ON a.article_id = m.article_id"
        " GROUP BY m.item_id"
    )
    with repo._connect() as conn:  # noqa: SLF001
        return {str(r["item_id"]): r["ts"] for r in conn.execute(sql).fetchall() if r["ts"]}


def window_article_ids(repo: Any, start: datetime) -> list[str]:
    from src.storage.event_time import DEDUP_ARTICLES, EVENT_TS_EXPR

    sql = (
        f"SELECT a.article_id AS article_id FROM {DEDUP_ARTICLES} a"  # noqa: S608
        f" WHERE {EVENT_TS_EXPR.format(a='a')} >= ?"
    )
    with repo._connect() as conn:  # noqa: SLF001
        return sorted({str(r["article_id"]) for r in conn.execute(sql, (start,)).fetchall()})


def article_rows(repo: Any, ids: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    with repo._connect() as conn:  # noqa: SLF001
        for i in range(0, len(ids), _CHUNK):
            chunk = ids[i : i + _CHUNK]
            ph = ",".join("?" for _ in chunk)
            for r in conn.execute(
                "SELECT article_id, MIN(title) AS title, MIN(feed_title) AS feed_title,"  # noqa: S608
                " MIN(published_at) AS published_at, MIN(summary) AS summary,"
                " MIN(subject_actor_ids) AS subject_actor_ids,"
                " MIN(subject_actor_source) AS subject_actor_source,"
                " MIN(subject_actor_confidence) AS subject_actor_confidence"
                f" FROM articles WHERE article_id IN ({ph}) GROUP BY article_id",
                chunk,
            ).fetchall():
                out[str(r["article_id"])] = dict(r)
    return out


def articles_events(repo: Any, event_ids: frozenset[str]) -> dict[str, frozenset[str]]:
    """記事 → 所属事象 (E2 の事象に属する記事を見分けるため、対象の事象だけ)。"""
    ids = sorted(event_ids)
    out: dict[str, set[str]] = {}
    with repo._connect() as conn:  # noqa: SLF001
        for i in range(0, len(ids), _CHUNK):
            chunk = ids[i : i + _CHUNK]
            ph = ",".join("?" for _ in chunk)
            for r in conn.execute(
                f"SELECT item_id, article_id FROM event_item_members WHERE item_id IN ({ph})",  # noqa: S608
                chunk,
            ).fetchall():
                out.setdefault(str(r["article_id"]), set()).add(str(r["item_id"]))
    return {k: frozenset(v) for k, v in out.items()}


def focus_ratios(repo: Any) -> dict[str, tuple[int, int]]:
    with repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            "SELECT f.item_id AS item_id, f.supporters AS s, f.outsiders AS o"
            " FROM event_item_focus f JOIN event_items i ON i.id = f.item_id"
            " WHERE i.merged_into IS NULL"
        ).fetchall()
    return {str(r["item_id"]): (int(r["s"]), int(r["o"])) for r in rows}


def event_layers(feats: dict[str, Any], repo: Any) -> dict[str, list[Unit]]:
    """C1・C2・C3・C5・対照 (窓内の複数記事の事象)。"""
    ratios = focus_ratios(repo)
    out: dict[str, list[Unit]] = {k: [] for k in ("C1", "C2", "C3", "C5", "control")}
    for eid, f in sorted(feats.items()):
        ev = (eid,)
        if is_c1(f.subjects):
            out["C1"].append(Unit("C1", eid, ev, evidence={"subjects": sorted(f.subjects)}))
        if is_c2(f.kinds):
            out["C2"].append(Unit("C2", eid, ev, evidence={"kinds": sorted(f.kinds)}))
        if is_c3(f.countries):
            out["C3"].append(Unit("C3", eid, ev, evidence={"countries": sorted(f.countries)}))
        s, o = ratios.get(eid, (0, 0))
        if is_c5(s, o):
            out["C5"].append(Unit("C5", eid, ev, evidence={"supporters": s, "outsiders": o}))
    pairs = control_pairs({e: (f.subjects, f.kinds) for e, f in feats.items()})
    for k in pairs:
        a, b = k.split("|")
        shared = sorted(feats[a].subjects & feats[b].subjects)
        out["control"].append(
            Unit(
                "control",
                k,
                (a, b),
                evidence={
                    "shared_subjects": shared,
                    "kinds": {a: sorted(feats[a].kinds), b: sorted(feats[b].kinds)},
                },
            )
        )
    return out


def c4_units(repo: Any, start: datetime) -> list[Unit]:
    from src.assessment.situation_track import registry_is_group
    from src.eventnews.relation_features import _trusted_subjects  # noqa: PLC2701

    ids = window_article_ids(repo, start)
    kinds: dict[str, str] = {}
    for i in range(0, len(ids), _CHUNK):
        kinds.update(repo.get_article_kinds(ids[i : i + _CHUNK]))
    cand = [a for a in ids if kinds.get(a, "") in ("advisory", "stats", "roundup")]
    rows = article_rows(repo, cand)
    is_group = registry_is_group()
    out: list[Unit] = []
    for aid in cand:
        subj = _trusted_subjects(rows.get(aid, {}), is_group)
        if is_c4(kinds[aid], subj):
            out.append(
                Unit(
                    "C4",
                    aid,
                    article_ids=(aid,),
                    evidence={"kind": kinds[aid], "subjects": sorted(subj)},
                )
            )
    return out


def c6_units(repo: Any, times: dict[str, datetime], start: datetime) -> tuple[list[Unit], int]:
    """同じ統合先に吸収された 2 事象の焦点が conflict の組。(conflict の組, 窓内の組の総数)。"""
    from src.eventnews.identity.focus import CLASS_CONFLICT, focus_from_dict, match_focus
    from src.ui.services.eventnews_focus_job import load_org_alias

    with repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            "SELECT target_id, absorbed_id, focus_json FROM event_merge_decisions"
            " WHERE undone_at IS NULL AND focus_json <> '' ORDER BY id"
        ).fetchall()
    latest: dict[str, tuple[str, str]] = {}
    for r in rows:
        latest[str(r["absorbed_id"])] = (str(r["target_id"]), str(r["focus_json"]))
    by_target: dict[str, list[str]] = {}
    for absorbed, (target, _) in sorted(latest.items()):
        if in_window(times.get(absorbed), start):
            by_target.setdefault(target, []).append(absorbed)
    alias = load_org_alias(repo)
    out: list[Unit] = []
    total = 0
    for target, members in sorted(by_target.items()):
        for a, b in combinations(sorted(members), 2):
            total += 1
            fa = focus_from_dict(json.loads(latest[a][1])["absorbed"])
            fb = focus_from_dict(json.loads(latest[b][1])["absorbed"])
            verdict = match_focus(fa, fb, org_alias=alias)
            if verdict.klass == CLASS_CONFLICT:
                out.append(
                    Unit(
                        "C6",
                        pair_key(a, b),
                        (a, b),
                        evidence={"target": target, "conflicts": list(verdict.conflicts)},
                    )
                )
    return out, total


def c10_units(repo: Any, times: dict[str, datetime], start: datetime) -> list[Unit]:
    """incident の線 (本番の導き方) のうち、片側がまとめ記事の事象の組。"""
    from src.eventnews.relation_features import actor_helpers, load_event_features
    from src.eventnews.relation_model import load_relation_model
    from src.eventnews.relation_pair_features import load_centroids
    from src.eventnews.relations import INCIDENT_TYPES, derive_with_model, index_relations

    nation_of, related = actor_helpers()
    events = load_event_features(repo, days=FEATURE_DAYS)
    by_id = {e.item_id: e for e in events}
    rels = derive_with_model(
        events,
        model=load_relation_model(),
        centroids=load_centroids(repo, events),
        nation_of=nation_of,
        related_actors=related,
    )
    shown = {id(r) for rs in index_relations(rels).values() for r in rs}
    out: dict[str, Unit] = {}
    for r in rels:
        if id(r) not in shown or r.rel_type not in INCIDENT_TYPES | {"incident"}:
            continue
        if not (in_window(times.get(r.a), start) and in_window(times.get(r.b), start)):
            continue
        round_sides = [x for x in (r.a, r.b) if by_id[x].roundup or "roundup" in by_id[x].kinds]
        if not round_sides:
            continue
        k = pair_key(r.a, r.b)
        out[k] = Unit(
            "C10", k, (r.a, r.b), evidence={"rel_type": r.rel_type, "roundup_side": round_sides}
        )
    return [out[k] for k in sorted(out)]


def article_payload(repo: Any, unit: Unit) -> dict[str, Any]:
    aid = unit.article_ids[0]
    row = article_rows(repo, [aid]).get(aid, {})
    return {
        "article_id": aid,
        "title": str(row.get("title") or ""),
        "source": str(row.get("feed_title") or ""),
        "published": str(row.get("published_at") or "")[:10],
        "summary": str(row.get("summary") or "")[:SUMMARY_MAX],
    }


def materials(repo: Any, unit: Unit) -> list[dict[str, Any]]:
    if unit.article_ids:
        return [article_payload(repo, unit)]
    return [event_payload(repo, e) for e in unit.event_ids]


def collect_layers(repo: Any, start: datetime) -> dict[str, list[Unit]]:
    from src.eventnews.relation_features import load_event_features

    times = event_times(repo)
    feats = {
        f.item_id: f
        for f in load_event_features(repo, days=FEATURE_DAYS)
        if len(f.member_ids) >= 2 and in_window(times.get(f.item_id), start)
    }
    layers = event_layers(feats, repo)
    layers["C4"] = c4_units(repo, start)
    layers["C6"], _ = c6_units(repo, times, start)
    layers["C10"] = c10_units(repo, times, start)
    return layers


def run(args: argparse.Namespace) -> int:
    assert_clean(*GUARDED_FILES)
    gate = require_prereg(PREREG, Path(__file__), OUT_DIR) if not args.count_only else {}
    now = datetime.now(UTC)
    start = window_start(now)
    repo = _repo()
    layers = collect_layers(repo, start)
    e2_pairs, e2_events, e2_shas = e2_exclusions(ROOT)
    guard_ids = guard_article_ids(ROOT)
    guard_ev = guarded_events(repo, guard_ids)
    art_ev = articles_events(repo, e2_events)
    summary: dict[str, Any] = {}
    selected: dict[str, list[Unit]] = {}
    for layer, n in LAYERS:
        kept, dropped = exclude_units(
            layers.get(layer, []),
            e2_pairs=e2_pairs,
            e2_events=e2_events,
            guarded_events=guard_ev,
            guarded_articles=guard_ids,
            article_events=art_ev,
        )
        selected[layer] = sample_units(kept, n, SEED, layer)
        summary[layer] = layer_summary(
            layer, n, len(layers.get(layer, [])), len(kept), dropped, len(selected[layer])
        )
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if args.count_only:
        return 0
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sample_path = OUT_DIR / "sample.jsonl"
    with sample_path.open("w", encoding="utf-8") as fh:
        for layer, _ in LAYERS:
            for u in selected[layer]:
                row = {
                    "layer": layer,
                    "unit": UNIT_TYPES[layer],
                    "key": u.key,
                    "event_ids": list(u.event_ids),
                    "article_ids": list(u.article_ids),
                    "evidence": dict(u.evidence),
                    "materials": materials(repo, u),
                }
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    write_json(
        OUT_DIR / "manifest.json",
        {
            **gate,
            "seed": SEED,
            "window": {"days": WINDOW_DAYS, "start": start.isoformat(), "end": now.isoformat()},
            "layers": summary,
            "layers_short_of_n": [k for k, v in summary.items() if v["short_of_n"]],
            "overlap": overlap_keys(selected),
            "sample_path": sample_path.relative_to(ROOT).as_posix(),
            "sample_rows": sum(len(v) for v in selected.values()),
            "sample_sha256": hashlib.sha256(sample_path.read_bytes()).hexdigest(),
            "guarded_events": len(guard_ev),
            "guard_articles": len(guard_ids),
            "exclusion_files_sha256": {
                **e2_shas,
                **{
                    rel: hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
                    for rel, _ in GUARD_FILES
                },
            },
            "created_at": now_iso(),
        },
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count-only", action="store_true")
    return run(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
