"""標本を作る (DB は SELECT のみ、生成なし)。

    uv run python -m scripts.n20_audit.collect t3   # 事象ニュース・状況総括・Spotlight 各 30
    (いずれも n19 の本番の出力)
    uv run python -m scripts.n20_audit.collect t4   # tr_raw (Sonnet の教師) の 30 チャンク

出力: data/eval/n20_t3/items_<task>.jsonl・data/eval/n20_t4/items.jsonl と各 manifest.json。
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from scripts.n20_audit import render
from scripts.n20_audit.common import (
    ACTOR_HOLDOUT,
    GUARD_V2,
    LEAK_ROOTS_V2,
    N19_SINCE,
    N20_TEACHER,
    N_PER_TASK,
    SEED_T3,
    SEED_T4,
    T3_DIR,
    T4_ALLOCATION,
    T4_DIR,
    TR_RAW,
    file_sha,
    psql_json,
    sha256,
    sql_list,
    write_json,
    write_jsonl,
)
from scripts.n20_audit.guard import Guard
from scripts.n20_audit.sampling import allocate, stratified_sample
from scripts.n20_audit.stats import split_claims

N19 = "kuebiko-sft:n19"
SPOT_BODY_HEAD = 4000
_N_SRC = re.compile(r"## 対象記事 \((\d+) 件")
_HEADLINE = re.compile(r"^\[\d+\] \*\*(.*?)\*\* \(", re.M)


def _guard() -> Guard:
    return Guard.load(GUARD_V2, LEAK_ROOTS_V2)


def _roots() -> dict[str, str]:
    parent = {
        r["id"]: r["merged_into"] or ""
        for r in psql_json("select id, merged_into from event_items")
    }

    def root(x: str) -> str:
        seen = set()
        while parent.get(x) and x not in seen:
            seen.add(x)
            x = parent[x]
        return x

    return {k: root(k) for k in parent}


def _item(
    task: str,
    item_id: str,
    stratum: str,
    material: str,
    output: str,
    complete: bool,
    meta: dict[str, Any],
) -> dict[str, Any]:
    return {
        "task": task,
        "item_id": item_id,
        "stratum": stratum,
        "material_complete": complete,
        "material": material,
        "output": output,
        "claims": split_claims(output),
        "material_sha": sha256(material),
        "output_sha": sha256(output),
        "meta": meta,
    }


def _ev_stratum(generated_at: str, n_src: int) -> str:
    period = (
        "a:09-29〜10-15"
        if generated_at < "2026-10-16"
        else ("b:10-16〜10-31" if generated_at < "2026-11-01" else "c:11-01〜")
    )
    size = "1" if n_src <= 1 else ("2-3" if n_src <= 3 else "4+")
    return f"{period}|src{size}"


def collect_event_news(
    guard: Guard, roots: dict[str, str]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = psql_json(
        "select distinct on (item_id) item_id, version, generated_at, headline, body_json,"
        " prompt_text"
        f" from event_item_versions where model = '{N19}' and prompt_text is not null"
        " order by item_id, version desc"
    )
    members: dict[str, list[str]] = {}
    for m in psql_json(
        "select item_id, article_id from event_item_members where item_id in"
        f" (select item_id from event_item_versions where model = '{N19}')"
    ):
        members.setdefault(m["item_id"], []).append(m["article_id"])
    excluded: Counter[str] = Counter()
    pool = []
    for r in rows:
        why = guard.reason(
            article_ids=members.get(r["item_id"], []),
            titles=_HEADLINE.findall(r["prompt_text"]),
            roots=[roots.get(r["item_id"], r["item_id"])],
        )
        if why:
            excluded[why.split(":")[0]] += 1
            continue
        n_src = _N_SRC.search(r["prompt_text"])
        r["n_src"] = int(n_src.group(1)) if n_src else 1
        pool.append(r)
    picked = stratified_sample(
        pool, lambda r: _ev_stratum(r["generated_at"], r["n_src"]), N_PER_TASK, SEED_T3
    )
    items = [
        _item(
            "event_news",
            f"{r['item_id']}@v{r['version']}",
            _ev_stratum(r["generated_at"], r["n_src"]),
            render.event_news_material(r["prompt_text"]),
            render.event_news_output(r["headline"], json.loads(r["body_json"])),
            True,
            {
                "generated_at": r["generated_at"],
                "n_src": r["n_src"],
                "prompt_sha": sha256(r["prompt_text"]),
            },
        )
        for r in picked
    ]
    return items, {
        "population": len(rows),
        "excluded": dict(excluded),
        "pool": len(pool),
        "strata": dict(Counter(i["stratum"] for i in items)),
    }


def _syn_stratum(r: dict[str, Any]) -> str:
    return f"{r['period_type']}|{'10-15以前' if r['period_start'] < '2026-10-16' else '10-16以降'}"


def collect_synthesis(guard: Guard) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = psql_json(
        "select id, period_type, period_start::text as period_start,"
        " generated_at::text as generated_at, headline, weight_section, chain_section,"
        " cog_section, spillover_section, pir_section, tradecraft"
        f" from status_synthesis where generated_at >= '{N19_SINCE}'"
        " and tradecraft->'grounded_estimate' is not null"
    )
    excluded: Counter[str] = Counter()
    pool = []
    for r in rows:
        aids = [
            ev["article_id"]
            for j in r["tradecraft"]["grounded_estimate"].get("judgments") or []
            for ev in j.get("evidence") or []
            if ev.get("article_id")
        ]
        why = guard.reason(article_ids=aids)
        r["n_guard_hits"] = sum(1 for a in set(aids) if guard.reason(article_ids=[a]))
        r["n_evidence_articles"] = len(set(aids))
        if why:
            excluded[why.split(":")[0]] += 1
            r["guard_reason"] = why
        pool.append(r)
    clean = [r for r in pool if "guard_reason" not in r]
    picked = stratified_sample(clean, _syn_stratum, N_PER_TASK, SEED_T3)
    items = [
        _item(
            "synthesis",
            f"syn:{r['id']}",
            _syn_stratum(r),
            render.synthesis_material(r["tradecraft"]),
            render.synthesis_output(r),
            True,
            {
                "period_type": r["period_type"],
                "period_start": r["period_start"],
                "generated_at": r["generated_at"],
                "n_evidence_articles": r["n_evidence_articles"],
            },
        )
        for r in picked
    ]
    return items, {
        "population": len(rows),
        "excluded": dict(excluded),
        "pool": len(clean),
        "guard_hits_per_window": [r["n_guard_hits"] for r in pool],
        "strata": dict(Counter(i["stratum"] for i in items)),
    }


def _spot_articles(ids: list[str]) -> dict[str, dict[str, Any]]:
    rows = psql_json(
        "select distinct on (article_id) article_id, title, url, feed_title, importance,"
        f" published_at::text as published_at, summary, left(body, {SPOT_BODY_HEAD}) as body_head"
        f" from articles where article_id in ({sql_list(ids)}) order by article_id, created_at desc"
    )
    return {r["article_id"]: r for r in rows}


def collect_spotlight(guard: Guard) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = psql_json(
        "select id, pir_id, period_type, period_start::text as period_start, generated_at::text as"
        " generated_at, headline, outlook, caveats, unknowns, key_events, article_count"
        f" from pir_spotlight where llm_model = '{N19}'"
    )
    excluded: Counter[str] = Counter()
    pool = []
    for r in rows:
        ke = r["key_events"] or []
        why = guard.reason(
            article_ids=[e.get("article_id", "") for e in ke],
            titles=[e.get("title", "") for e in ke],
            urls=[e.get("url", "") for e in ke],
        )
        if why:
            excluded[why.split(":")[0]] += 1
            continue
        pool.append(r)
    picked = stratified_sample(pool, lambda r: r["pir_id"], N_PER_TASK, SEED_T3)
    arts = _spot_articles([e["article_id"] for r in picked for e in r["key_events"] or []])
    items = []
    for r in picked:
        prev = psql_json(
            "select headline, outlook from pir_spotlight where pir_id = "
            f"{sql_list([r['pir_id']])} and period_type = {sql_list([r['period_type']])}"
            f" and period_start < '{r['period_start']}' order by period_start desc limit 1"
        )
        ke_arts = [{**e, **arts.get(e["article_id"], {})} for e in r["key_events"] or []]
        items.append(
            _item(
                "spotlight",
                f"spot:{r['id']}",
                r["pir_id"],
                render.spotlight_material(ke_arts, prev[0] if prev else None),
                render.spotlight_output(r),
                False,
                {
                    "pir_id": r["pir_id"],
                    "period_start": r["period_start"],
                    "generated_at": r["generated_at"],
                    "article_count": r["article_count"],
                    "n_key_events": len(ke_arts),
                    "has_previous": bool(prev),
                },
            )
        )
    return items, {
        "population": len(rows),
        "excluded": dict(excluded),
        "pool": len(pool),
        "strata": dict(Counter(i["stratum"] for i in items)),
    }


def run_t3() -> int:
    guard = _guard()
    roots = _roots()
    collectors: dict[str, Callable[[], tuple[list[dict[str, Any]], dict[str, Any]]]] = {
        "event_news": lambda: collect_event_news(guard, roots),
        "synthesis": lambda: collect_synthesis(guard),
        "spotlight": lambda: collect_spotlight(guard),
    }
    manifest: dict[str, Any] = {
        "created_at": datetime.now(UTC).isoformat(),
        "seed": SEED_T3,
        "n19_since": N19_SINCE,
        "guard": {
            "path": str(GUARD_V2.relative_to(N20_TEACHER.parents[2])),
            "sha256": file_sha(GUARD_V2),
        },
        "leak_roots": {
            "path": str(LEAK_ROOTS_V2.relative_to(N20_TEACHER.parents[2])),
            "sha256": file_sha(LEAK_ROOTS_V2),
        },
        "tasks": {},
    }
    for task, fn in collectors.items():
        items, info = fn()
        path = T3_DIR / f"items_{task}.jsonl"
        write_jsonl(path, items)
        claims = [len(i["claims"]) for i in items]
        manifest["tasks"][task] = {
            **info,
            "n": len(items),
            "claims_total": sum(claims),
            "claims_per_item_mean": round(sum(claims) / max(1, len(claims)), 1),
            "file_sha256": file_sha(path),
            "item_ids": [i["item_id"] for i in items],
        }
        print(task, len(items), info.get("excluded"), "claims", sum(claims), flush=True)
    write_json(T3_DIR / "manifest_items.json", manifest)
    return 0


def _t4_population() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    sys.path.insert(0, str(N20_TEACHER))
    import tr_finalize  # type: ignore[import-not-found]  # noqa: PLC0415
    from tr_collect import load_candidates  # type: ignore[import-not-found]  # noqa: PLC0415

    pool: list[dict[str, Any]] = []
    stats: dict[str, Any] = {}
    for task in ("translate", "actor_sync"):
        seen: set[str] = set()
        cands = []
        for tier in ("main", "reserve"):
            for c in load_candidates(task, tier):
                if c["source_sha"] not in seen:  # actor_sync は tier を見ないので重複する
                    seen.add(c["source_sha"])
                    cands.append(c)
        ev = tr_finalize.evaluate(task, cands)
        acc = [r for r in ev if r["accepted_idx"] is not None and not r["n_a"]]
        stats[task] = {
            "candidates": len(ev),
            "with_teacher": sum(1 for r in ev if r["tries"]),
            "gate_accepted": len(acc),
        }
        for r in acc:
            t = r["tries"][r["accepted_idx"]]
            pool.append(
                {
                    "kind": task,
                    "source": r["source"],
                    "output": t["output"],
                    "source_sha": r["source_sha"],
                    "article_id": r.get("article_id", ""),
                    "mitre_id": r.get("mitre_id", ""),
                    "attempt": t.get("attempt", 1),
                    "tier": r.get("tier", ""),
                    "teacher": t.get("teacher", ""),
                }
            )
    return pool, stats


def _kind_of(p: dict[str, Any]) -> str:
    return str(p["kind"])


def run_t4() -> int:
    guard = _guard()
    pool, stats = _t4_population()
    aids = sorted({p["article_id"] for p in pool if p["article_id"]})
    meta = {
        r["article_id"]: r
        for r in psql_json(
            "select distinct on (article_id) article_id, title, url from articles"
            f" where article_id in ({sql_list(aids)}) order by article_id, created_at desc"
        )
    }
    holdout = {h["mitre_id"] for h in json.loads(ACTOR_HOLDOUT.read_text(encoding="utf-8"))}
    excluded: Counter[str] = Counter()
    clean = []
    for p in pool:
        m = meta.get(p["article_id"], {})
        why = guard.reason(
            article_ids=[p["article_id"]] if p["article_id"] else [],
            titles=[m.get("title") or ""],
            urls=[m.get("url") or ""],
        )
        if not why and p["mitre_id"] in holdout:
            why = "actor_holdout:" + p["mitre_id"]
        if why:
            excluded[why.split(":")[0]] += 1
            continue
        clean.append(p)
    sizes = Counter(p["kind"] for p in clean)
    alloc = {k: min(v, sizes[k]) for k, v in T4_ALLOCATION.items()}
    picked: list[dict[str, Any]] = []
    for kind, n in alloc.items():
        picked.extend(
            stratified_sample([p for p in clean if p["kind"] == kind], _kind_of, n, SEED_T4)
        )
    items = [
        {
            **p,
            "task": "translation",
            "item_id": f"{p['kind']}:{p['source_sha'][:16]}",
            "stratum": p["kind"],
            "output_sha": sha256(p["output"]),
        }
        for p in picked
    ]
    write_jsonl(T4_DIR / "items.jsonl", items)
    write_json(
        T4_DIR / "manifest_items.json",
        {
            "created_at": datetime.now(UTC).isoformat(),
            "seed": SEED_T4,
            "allocation": alloc,
            "population_sizes": dict(sizes),
            "proportional_reference": allocate(dict(sizes), 30),
            "tr_raw": {
                "path": "data/mlx/n20_teacher/work/tr_raw.jsonl",
                "sha256": file_sha(TR_RAW),
            },
            "guard_sha256": file_sha(GUARD_V2),
            "leak_roots_sha256": file_sha(LEAK_ROOTS_V2),
            "gate": stats,
            "excluded": dict(excluded),
            "selection": (
                "source_sha ごとに tr_finalize.evaluate の関門"
                " (tr_gate.check_translation) を最初に通った試行"
            ),
            "item_ids": [i["item_id"] for i in items],
            "file_sha256": file_sha(T4_DIR / "items.jsonl"),
        },
    )
    print(
        "t4", dict(Counter(i["kind"] for i in items)), "excluded", dict(excluded), stats, flush=True
    )
    return 0


def main(argv: list[str]) -> int:
    if argv[1:2] == ["t3"]:
        return run_t3()
    if argv[1:2] == ["t4"]:
        return run_t4()
    print(__doc__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
