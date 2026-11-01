"""gold:imp_precheck_s26 の母集団と R18 の閉包 (prereg §2.2・§7)。DB は読み取りのみ。

data/eval/triage_fresh_s26/build.py と同じ部品 (data/eval/rp9_frozen/build.py の fetch_* と
scripts/replay/_closure.py) を使い、汚染の源だけを s26 の学習の全課題 + 台帳の tainted + 他の判定集に替える。
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import warnings
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from scripts.s26_imp.common import (
    DATA_ROOT,
    DATASET_DRY_REL,
    GUARD_S26_REL,
    PLAN_REL,
    ROOT,
    read_jsonl,
)

WINDOW = (
    "2026-10-13T00:00:00+09:00",
    "2026-11-02T00:00:00+09:00",
)  # prereg §12 (11-02) で 10-20 → 10-13
UNIVERSE_FROM = "2026-09-01T00:00:00+09:00"
BODY_MIN_CHARS = 100
TITLE_MIN = 8
MAX_COMPONENT_SHARE = 0.30
ID_RE = re.compile(r"[a-z0-9][a-z0-9._\-]*:[0-9a-f]{16}")
OTHER_GOLD_FILES = (
    "data/eval/triage_fresh_s26/gold.jsonl",
    "data/eval/triage_fresh_s26/sample.jsonl",
    "data/eval/triage_fresh_s26/supp_gold.jsonl",
    "data/eval/triage_fresh_s26/supp_sample.jsonl",
)


def load_modules() -> tuple[ModuleType, ModuleType]:
    """(rp9 の build モジュール, build_dataset_s24)。どちらも data/ (gitignore) にある。"""
    os.environ.setdefault("KUEBIKO_ROOT", str(DATA_ROOT))
    for p in (str(DATA_ROOT / "data/mlx"), str(ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    path = DATA_ROOT / "data/eval/rp9_frozen/build.py"
    spec = importlib.util.spec_from_file_location("rp9_build", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"読み込めない: {path}")
    rp9 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rp9)
    return rp9, __import__("build_dataset_s24")


def collapse_latest(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """created_at 昇順の行を article_id ごとの最新の 1 行に畳む (後の行が勝つ)。"""
    pool: dict[str, dict[str, Any]] = {}
    for r in rows:
        pool[r["article_id"]] = r
    return pool


def window_from(start_date: str) -> tuple[str, str]:
    """開始日 (YYYY-MM-DD, JST) → 窓。終端は prereg と同じ 11-02 00:00 (排他)。"""
    return (f"{start_date}T00:00:00+09:00", WINDOW[1])


def fetch_pool(
    c: Any, ev: str, window: tuple[str, str] = WINDOW
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """窓内・posted・duplicate_of IS NULL の最新 1 行 (本文 100 字以上)。"""
    rows = c.execute(
        "SELECT a.article_id, a.title, a.original_title, a.url, a.feed_title, a.importance,"
        f" a.category, a.body, a.created_at, {ev} AS ev FROM articles a"
        f" WHERE a.status = 'posted' AND a.duplicate_of IS NULL AND {ev} >= ? AND {ev} < ?"
        " ORDER BY a.created_at",
        window,
    ).fetchall()
    latest = collapse_latest(rows)
    pool = {a: r for a, r in latest.items() if len(r["body"] or "") >= BODY_MIN_CHARS}
    counts = {
        "rows_in_window": len(rows),
        "unique_articles": len(latest),
        "duplicate_rows_collapsed": len(rows) - len(latest),
        "body_under_min_chars": len(latest) - len(pool),
        "population": len(pool),
    }
    return pool, counts


def _payload(p: Path) -> Any:
    text = p.read_text("utf-8")
    if p.suffix == ".jsonl":
        return [json.loads(x) for x in text.split("\n") if x.strip()]
    return json.loads(text)


def _from_files(s24mod: ModuleType, files: list[Path]) -> tuple[set[str], set[str]]:
    ids: set[str] = set()
    titles: set[str] = set()
    for p in files:
        text = p.read_text("utf-8", errors="replace")
        ids.update(ID_RE.findall(text))
        if p.suffix in (".json", ".jsonl"):
            try:
                s24mod._walk_guard(_payload(p), ids, titles)  # noqa: SLF001
            except ValueError:
                continue
    return ids, titles


@dataclass(frozen=True)
class Sources:
    ids: dict[str, set[str]]
    titles: dict[str, set[str]]
    corpus: str
    missing_tainted: tuple[str, ...]


def load_sources(rp9: ModuleType, s24mod: ModuleType) -> Sources:
    """汚染の源ごとの (id 集合, 見出し・URL の集合) と、s26 の学習の全文 (部分一致用)。"""
    from scripts.replay._gate import load_tainted_registry

    ids: dict[str, set[str]] = {}
    titles: dict[str, set[str]] = {}

    g = json.loads((DATA_ROOT / GUARD_S26_REL).read_text("utf-8"))
    ids["guard_s26"] = set(g["article_ids"])
    titles["guard_s26"] = set(g["titles"]) | set(g["urls"])

    gi, gt, _src = rp9.guard_sets()
    ids["s24_s25_guard_and_plan_gold"], titles["s24_s25_guard_and_plan_gold"] = set(gi), set(gt)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        reg = load_tainted_registry(DATA_ROOT / PLAN_REL, DATA_ROOT)
    files = [DATA_ROOT / p for p in sorted(reg.paths) if (DATA_ROOT / p).is_file()]
    ti, tt = _from_files(s24mod, files)
    ti |= {x.split(":", 1)[1] for x in reg.ids if x.startswith("article_id:")}
    ids["plan_tainted_gold_sets"], titles["plan_tainted_gold_sets"] = ti, tt

    oi, ot = _from_files(s24mod, [DATA_ROOT / p for p in OTHER_GOLD_FILES])
    ids["triage_fresh_s26_and_supp"], titles["triage_fresh_s26_and_supp"] = oi, ot

    frozen = read_jsonl(DATA_ROOT / "data/eval/rp9_frozen/frozen_articles.jsonl")
    ek = json.loads((DATA_ROOT / "data/eval/rp9_frozen/exclusion_keys.json").read_text("utf-8"))
    ids["rp9_frozen"] = {f["article_id"] for f in frozen} | set(ek["closure_article_ids"])
    titles["rp9_frozen"] = set()

    parts: list[str] = []
    for split in ("train", "valid"):
        with (DATA_ROOT / DATASET_DRY_REL / f"{split}.jsonl").open(encoding="utf-8") as fh:
            for line in fh:
                parts.append("\n".join(m["content"] for m in json.loads(line)["messages"]))
    corpus = "\n\x00\n".join(parts)
    ids["s26_training_rows"] = set(ID_RE.findall(corpus))
    titles["s26_training_rows"] = set()
    return Sources(ids, titles, corpus, tuple(reg.missing))


@dataclass(frozen=True)
class ClosureResult:
    eligible: list[str]
    excluded_by_source: dict[str, int]
    excluded_union: int
    diagnostics: dict[str, Any]


def compute_eligible(
    c: Any, rp9: ModuleType, s24mod: ModuleType, pool: dict[str, dict[str, Any]], ev: str
) -> ClosureResult:
    """pool のうち、汚染の源と R18 の成分で交わらない記事。"""
    from scripts.replay._common import (
        COS_THRESHOLD,
        R18_VERSION,
        assert_components_ok,
        build_components,
        component_diagnostics,
    )
    from src.eventnews.relation_features import FOCUS_CVE_MAX

    src = load_sources(rp9, s24mod)
    uni = rp9.fetch_articles(c, f"{ev} >= ? AND {ev} < ?", (UNIVERSE_FROM, WINDOW[1]))
    roots, cves, orgs = rp9.fetch_keys(c, sorted(uni))
    extra = rp9.key_neighbors(c, roots, cves, orgs) - set(uni)
    for s in src.ids.values():
        extra |= {i for i in s if i not in uni}
    extra |= {i for i in pool if i not in uni}
    if extra:
        uni.update(rp9.fetch_articles(c, "a.article_id = ANY(?)", (sorted(extra),)))
        r2, c2, o2 = rp9.fetch_keys(c, sorted(extra))
        for m, dst in ((r2, roots), (c2, cves), (o2, orgs)):
            for k, v in m.items():
                dst[k] |= v
    ids_all = sorted(uni)
    emb = rp9.fetch_embeddings(c, ids_all)
    for d in (cves, orgs):
        for a in list(d):
            if len(d[a]) > FOCUS_CVE_MAX:
                d[a] = set()
    root_first = {a: min(v) for a, v in roots.items() if v}
    cve_keys = {
        a: frozenset(cves.get(a, set()) | {f"ROOT#{r}" for r in roots.get(a, set())})
        for a in ids_all
    }
    comp = build_components(
        ids_all,
        root_of=root_first,
        cves=cve_keys,
        victim_orgs={a: frozenset(v) for a, v in orgs.items()},
        embeddings={a: v.tolist() for a, v in emb.items()},
        cos_threshold=COS_THRESHOLD,
    )
    n_comp, largest, share = component_diagnostics(comp)
    assert_components_ok(comp, MAX_COMPONENT_SHARE)

    members: dict[str, list[str]] = defaultdict(list)
    for a, cc in comp.items():
        members[cc].append(a)
    pool_comps = {comp[a] for a in pool if a in comp}
    bad: dict[str, set[str]] = {k: set() for k in src.ids}
    for a in (x for cc in pool_comps for x in members[cc]):
        r = uni[a]
        tt = ({r.get("title") or "", r.get("original_title") or "", r.get("url") or ""}) - {""}
        for k in src.ids:
            if a in src.ids[k] or (tt & src.titles[k]):
                bad[k].add(comp[a])
        if any(len(v) >= TITLE_MIN and v in src.corpus for v in tt):
            bad["s26_training_rows"].add(comp[a])
    excluded = {k: sum(1 for a in pool if comp.get(a) in v) for k, v in bad.items()}
    union_bad = set().union(*bad.values())
    eligible = sorted(a for a in pool if comp.get(a) not in union_bad)
    diag = {
        "r18_version": R18_VERSION,
        "cos": COS_THRESHOLD,
        "n_components": n_comp,
        "largest_component": largest,
        "largest_share": round(share, 4),
        "universe_articles": len(ids_all),
        "with_summary_embedding": len(emb),
        "tainted_files_missing": list(src.missing_tainted),
        "source_sizes": {
            k: {"ids": len(src.ids[k]), "titles": len(src.titles[k])} for k in src.ids
        },
    }
    return ClosureResult(eligible, excluded, len(pool) - len(eligible), diag)


def stratified_sample(
    eligible: list[str], importance_of: dict[str, str | None], *, seed: int
) -> tuple[list[str], dict[str, dict[str, int]]]:
    """保存の importance で層化: high ≤50 / medium 50 / low 50 (乱数種 seed)。"""
    import random

    caps = {"high": 50, "medium": 50, "low": 50}
    rng = random.Random(seed)
    picked: list[str] = []
    counts: dict[str, dict[str, int]] = {}
    pool_counts = Counter(importance_of.get(a) for a in eligible)
    for stratum, cap in caps.items():
        members = sorted(a for a in eligible if importance_of.get(a) == stratum)
        take = members if len(members) <= cap else rng.sample(members, cap)
        picked.extend(sorted(take))
        counts[stratum] = {"population_after_r18": len(members), "sampled": len(take)}
    counts["_other_importance"] = {
        "population_after_r18": sum(pool_counts.values())
        - sum(v["population_after_r18"] for v in counts.values()),
        "sampled": 0,
    }
    return picked, counts


def teacher_event_times(c: Any, ev: str, dataset: Path) -> dict[str, Any]:
    """s26 の要約の教師の記事の事象時刻 (見出しで DB の記事に引く。引けない行は件数の差で分かる)。"""
    import statistics
    from datetime import datetime

    titles: set[str] = set()
    n_rows = 0
    for split in ("train", "valid"):
        for r in read_jsonl(dataset / f"{split}.jsonl"):
            if "[task: summary]" not in r["messages"][0]["content"]:
                continue
            n_rows += 1
            m = re.search(
                r"【記事タイトル】\n(.*?)\n\n【記事 ソース】", r["messages"][-2]["content"], re.S
            )
            if m:
                titles.add(m.group(1))
    rows = c.execute(
        f"SELECT a.article_id, {ev} AS ev FROM articles a WHERE a.title = ANY(?)", (sorted(titles),)
    ).fetchall()
    by_id = {r["article_id"]: r["ev"] for r in rows}
    stamps = sorted(
        v if isinstance(v, datetime) else datetime.fromisoformat(str(v)) for v in by_id.values()
    )
    out: dict[str, Any] = {
        "summary_rows": n_rows,
        "distinct_titles": len(titles),
        "matched_articles": len(stamps),
    }
    if stamps:
        out |= {
            "min": stamps[0].isoformat(),
            "median": statistics.median_low(stamps).isoformat(),
            "max": stamps[-1].isoformat(),
        }
        for d in ("2026-10-01", "2026-10-06", "2026-10-13", "2026-10-20"):
            cut = datetime.fromisoformat(f"{d}T00:00:00+09:00")
            out[f"share_on_or_after_{d}"] = round(sum(s >= cut for s in stamps) / len(stamps), 4)
    return out
