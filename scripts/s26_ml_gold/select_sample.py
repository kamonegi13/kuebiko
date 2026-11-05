#!/usr/bin/env python3
"""S26-ML-GOLD の標本の選び出し (prereg docs/replay_prereg/s26_ml_gold.md)。

決定論 (乱数種 20261106)。本番 DB は読むだけ、LLM・Opus・埋込の計算は呼ばない。本文は書かない (sha256 だけ)。
ホストで実行 (relation と deep_dive は本番の関数を read-only の接続で使う):

    export DATABASE_URL=$(docker exec kuebiko printenv DATABASE_URL | sed -E 's#@[^/]+/#@127.0.0.1:5433/#')
    uv run python -m scripts.s26_ml_gold.select_sample --sets detect,sevc,relation,deep_dive --window 1 [--dry-run]
    uv run python -m scripts.s26_ml_gold.select_sample --sets deep_dive --window 2   # 12-14 までに窓ごと
"""

# ruff: noqa: E501
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "gpu_window"))

import detect_closure_core as cc  # noqa: E402
import detect_rejudge_closure as drc  # noqa: E402

from scripts.replay._closure import R18_VERSION, component_diagnostics  # noqa: E402
from scripts.replay._gate import require_prereg, sha256_file  # noqa: E402
from scripts.replay._manifest import assert_no_body  # noqa: E402
from scripts.s26_ml_gold import core, db  # noqa: E402
from scripts.s26_ml_gold import population as pop  # noqa: E402
from scripts.s26_ml_gold.paths import DATA_ROOT, OUT, out_dir  # noqa: E402

PREREG = "s26_ml_gold"
SETS = ("detect", "sevc", "relation", "deep_dive")
TRAIN_FILES: dict[str, tuple[str, ...]] = {
    "detect": (
        "data/mlx/detect_goldset.jsonl",
        "data/mlx/detect_gold_kinds.jsonl",
        "data/mlx/detect_heldout_kinds.jsonl",
        "data/mlx/detect_labels.json",
    ),
    "sevc": (
        "data/eval/severity_gold_v5_final.jsonl",
        "data/eval/severity_gold_v5.jsonl",
        "data/eval/severity_gold_v4.jsonl",
    ),
    "deep_dive": ("data/mlx/deep_dive_labels.jsonl", "data/mlx/deep_dive_goldset.jsonl"),
}
TEACHER_GLOBS = {"detect": "data/mlx/teacher/detect*.jsonl"}
REL_GOLD_GLOB = "data/mlx/event_relations_goldset*.jsonl"
S26_DATA_GLOB = "data/mlx/dataset_s26*/*.jsonl"
PRIOR_ARMS_GLOB = "data/eval/detect_decompose/arms_*.jsonl"


def train_side(known: set[str]) -> tuple[dict[str, set[str]], dict[str, Any], set[str]]:
    """判定集 → 学習・調整側の記事 (DB に実在する id だけ)、出どころ、relation の学習の事象の鍵。"""
    sides: dict[str, set[str]] = {}
    src: dict[str, Any] = {}
    for name, files in TRAIN_FILES.items():
        paths = [DATA_ROOT / f for f in files]
        if name in TEACHER_GLOBS:
            paths += sorted(DATA_ROOT.glob(TEACHER_GLOBS[name]))
        found: set[str] = set()
        for p in paths:
            if p.is_file():
                ids = cc.id_tokens(p.read_text(encoding="utf-8")) & known
                src[f"{name}:{p.name}"] = {"ids": len(ids), "sha256": sha256_file(p)}
                found |= ids
        sides[name] = found
    rel_keys: set[str] = set()
    for p in sorted(DATA_ROOT.glob(REL_GOLD_GLOB)):
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                key = str(json.loads(line).get("key") or "")
                rel_keys |= {k for k in key.split("|") if k}
        src[f"relation:{p.name}"] = {"sha256": sha256_file(p)}
    members = db.members_of_items(sorted(rel_keys))
    sides["relation"] = set().union(*members.values()) if members else set()
    src["relation:event_keys"] = len(rel_keys)
    return sides, src, rel_keys


def teacher_index() -> tuple[core.TeacherIndex, dict[str, str]]:
    files = sorted(DATA_ROOT.glob(S26_DATA_GLOB))
    text = "\n".join(p.read_text(encoding="utf-8") for p in files)
    return core.TeacherIndex(text), {str(p.relative_to(DATA_ROOT)): sha256_file(p) for p in files}


def closure(base: set[str]) -> tuple[dict[str, str], dict[str, Any]]:
    """R18 の閉包 (DETECT-REJUDGE の段を L0 から、最大成分 < 0.30 の最初の段)。"""
    ents = drc.entity_values()
    roots = drc.event_roots()
    freqs = {
        "cve": cc.value_frequencies(ents["cve"]),
        "victim_org": cc.value_frequencies(ents["victim_org"]),
        "root": cc.value_frequencies(roots[1]),
    }
    emb: dict[str, list[float]] = {}
    steps: list[dict[str, Any]] = []
    for step in cc.LADDER:
        comp, universe = drc.run_step(step, base, ents, freqs, roots, emb)
        n, largest, share = component_diagnostics(comp)
        steps.append(
            {
                "step": step.name,
                "universe": len(universe),
                "n_components": n,
                "largest": largest,
                "share": share,
            }
        )
        print(json.dumps(steps[-1]), flush=True)
        if share < cc.MAX_COMPONENT_SHARE:
            return comp, {"chosen_step": step.name, "steps": steps}
    raise SystemExit("R18: どの段でも最大成分が 0.30 未満にならない — 判定集を作らない (prereg §6)")


def prior_judge_articles() -> set[str]:
    out: set[str] = set()
    for p in sorted(DATA_ROOT.glob(PRIOR_ARMS_GLOB)):
        for line in p.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line) if line.strip() else {}
            for o in rec.get("opened", []) or []:
                out |= {str(a) for a in o.get("article_ids", [])}
    return out


def filter_units(
    units: list[pop.Unit],
    arts: dict[str, dict[str, Any]],
    dirty: set[str],
    teacher: core.TeacherIndex,
    rel_keys: set[str],
) -> tuple[list[pop.Unit], Counter[str]]:
    """§2.5 の除外 (数える)。relation はラベルに使う構成記事を選んで extra に足す。"""
    kept: list[pop.Unit] = []
    excl: Counter[str] = Counter()
    for u in units:
        if u.extra.get("a") in rel_keys or u.extra.get("b") in rel_keys:
            excl["relation_direct_overlap"] += 1
            continue
        if "members_a" in u.extra:
            ma = pop.pick_members(list(u.extra["members_a"]), arts)
            mb = pop.pick_members(list(u.extra["members_b"]), arts)
            if not ma or not mb:
                excl["unreadable_member"] += 1
                continue
            u = pop.Unit(
                u.key,
                u.stratum,
                u.articles,
                {**u.extra, "label_members_a": ma, "label_members_b": mb},
            )
        elif not (arts.get(u.key, {}).get("body") or "").strip():
            excl["unreadable_body"] += 1
            continue
        if any(
            teacher.hits(
                article_id=a, url=core.url_of(a), title=str(arts.get(a, {}).get("title") or "")
            )
            for a in u.articles
        ):
            excl["s26_teacher"] += 1
            continue
        if core.group_contaminated(u.articles, dirty):
            excl[f"r18:{u.stratum}"] += 1
            continue
        kept.append(u)
    return kept, excl


def sample_rows(
    name: str,
    units: list[pop.Unit],
    plan: Sequence[tuple[str, int]],
    seed: int,
    arts: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by = {u.key: u for u in units}
    pools: dict[str, list[str]] = {}
    for u in units:
        pools.setdefault(u.stratum, []).append(u.key)
    avail = {h: len(v) for h, v in pools.items()}
    targets = core.allocate(avail, plan)
    drawn = core.draw(pools, targets, plan, seed)
    w = core.weights(avail, {h: len(v) for h, v in drawn.items()})
    rows = []
    for h, keys in drawn.items():
        for k in keys:
            u = by[k]
            label_ids = list(u.extra.get("label_members_a", [])) + list(
                u.extra.get("label_members_b", [])
            ) or [u.key]
            rows.append(
                {
                    "key": k,
                    "set": name,
                    "stratum": h,
                    "weight": w[h],
                    **{x: v for x, v in u.extra.items() if x not in ("members_a", "members_b")},
                    "body_sha256": {
                        a: core.sha256_text(str(arts[a]["body"] or "")) for a in label_ids
                    },
                }
            )
    rows.sort(key=lambda r: str(r["key"]))
    return rows, {
        "population_by_stratum": avail,
        "targets": targets,
        "sample_by_stratum": {h: len(v) for h, v in drawn.items()},
        "weights": w,
    }


def run(sets: list[str], window_no: int, dry_run: bool) -> int:
    rec = None
    try:
        rec = require_prereg(PREREG, ROOT, run_started_at=datetime.now(UTC), caller=Path(__file__))
    except Exception as exc:  # noqa: BLE001
        if not dry_run:
            raise
        print(f"[dry-run] prereg の関門は通らない: {exc}")
    repo = db.open_repo() if {"relation", "deep_dive"} & set(sets) else None
    units: dict[str, list[pop.Unit]] = {}
    meta: dict[str, Any] = {}
    if "detect" in sets:
        units["detect"] = pop.detect_units()
    if "sevc" in sets:
        units["sevc"] = pop.sevc_units()
    if "relation" in sets:
        units["relation"], meta["relation"] = pop.relation_units(repo)
    if "deep_dive" in sets:
        if core.DD_WINDOW_ENDS[window_no - 1] > datetime.now(UTC):
            raise SystemExit(f"deep_dive の窓 {window_no} はまだ閉じていない")
        units["deep_dive"], meta["deep_dive"] = pop.deep_dive_units(repo, window_no)
    known = drc.all_article_ids()
    sides, train_src, rel_keys = train_side(known)
    all_articles = {a for us in units.values() for u in us for a in u.articles}
    comp, closure_meta = closure(all_articles | set().union(*sides.values()))
    teacher, teacher_files = teacher_index()
    arts = db.fetch_articles(sorted(all_articles))
    prior = prior_judge_articles()
    for name, us in units.items():
        dirty = core.contaminated(all_articles, comp, sides[name])
        kept, excl = filter_units(us, arts, dirty, teacher, rel_keys)
        if name == "deep_dive":
            plan, seed, d = (
                core.dd_allocation(window_no),
                core.dd_seed(window_no),
                out_dir(name, window_no),
            )
        else:
            plan, seed, d = core.ALLOCATION[name], core.SEED, out_dir(name)
        rows, stats = sample_rows(name, kept, plan, seed, arts)
        counts = {
            "population_before_exclusion": dict(Counter(u.stratum for u in us)),
            "excluded": dict(excl),
            **stats,
            "detect_judge_overlap": sum(1 for r in rows if r["key"] in prior)
            if name == "detect"
            else None,
        }
        print(name, json.dumps(counts, ensure_ascii=False))
        if dry_run or rec is None:
            continue
        d.mkdir(parents=True, exist_ok=True)
        (d / "sample.jsonl").write_text(core.jsonl_text(rows), "utf-8")
        (d / "population.jsonl").write_text(
            core.jsonl_text(
                {
                    "key": u.key,
                    "stratum": u.stratum,
                    **{k: v for k, v in u.extra.items() if k in ("rank", "p_s3", "a", "b")},
                }
                for u in sorted(kept, key=lambda x: x.key)
            ),
            "utf-8",
        )
        label_ids = sorted({a for r in rows for a in r["body_sha256"]})
        (d / "exclusion_keys.json").write_text(
            json.dumps(
                {
                    "article_ids": label_ids,
                    "urls": sorted({core.url_of(a) for a in label_ids} - {""}),
                    "titles": sorted({str(arts[a]["title"] or "") for a in label_ids} - {""}),
                },
                ensure_ascii=False,
                indent=1,
            ),
            "utf-8",
        )
        manifest = {
            "set": name,
            "item": "S26-ML-GOLD",
            "built_at": datetime.now(UTC).isoformat(),
            "prereg_path": rec.path,
            "prereg_git_sha": rec.git_sha,
            "prereg_blob_sha256": rec.blob_sha256,
            "head_sha": rec.head_sha,
            "seed": seed,
            "window": [core.WINDOW_START.isoformat(), core.WINDOW_END.isoformat()],
            "meta": meta.get(name),
            "r18_version": R18_VERSION,
            "closure": closure_meta,
            "max_component_share": cc.MAX_COMPONENT_SHARE,
            "train_side_sizes": {k: len(v) for k, v in sides.items()},
            "train_sources": train_src,
            "s26_teacher_files": teacher_files,
            "counts": counts,
            "sample_sha256": sha256_file(d / "sample.jsonl"),
            "population_sha256": sha256_file(d / "population.jsonl"),
            "body_sha256_definition": "sha256 of stored body (utf-8, unstripped)",
        }
        assert_no_body(manifest)
        (d / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + "\n", "utf-8"
        )
        print(name, "sample.jsonl sha256", manifest["sample_sha256"], len(rows))
    if dry_run:
        print(f"[dry-run] 何も書かない (出力先 {OUT})")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--sets", default=",".join(SETS))
    ap.add_argument("--window", type=int, default=1, help="deep_dive の窓 (1〜7)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    sets = [s for s in a.sets.split(",") if s]
    bad = set(sets) - set(SETS)
    if bad:
        ap.error(f"unknown sets: {sorted(bad)}")
    return run(sets, a.window, a.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
