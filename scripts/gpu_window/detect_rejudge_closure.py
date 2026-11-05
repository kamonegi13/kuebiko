#!/usr/bin/env python3
"""DETECT-REJUDGE の R18.2: 凍結した窓の記事で閉包の段を選び、汚染記事を固定する (2 腕の実行の前)。

仕様は docs/replay_prereg/detect_decompose_rejudge.md §7。ホストで実行する。本番 DB は
read-only セッションの SELECT だけ (docker exec psql)。Opus も Ollama も使わない。

    uv run python scripts/gpu_window/detect_rejudge_closure.py \\
        --inputs-dir /path/to/data/eval/detect_rejudge
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))
import detect_closure_core as cc  # noqa: E402

from scripts.replay._closure import (  # noqa: E402
    build_components,
    component_diagnostics,
    resolve_roots,
)

EMBED_MODEL = "snowflake-arctic-embed2"
PLAN_NAME = "closure_plan.json"
PRIOR_ARMS = Path("data/eval/detect_decompose/arms_20261104T022916Z.jsonl")
TRAIN_FILES = (
    Path("data/mlx/detect_goldset.jsonl"),
    Path("data/mlx/detect_gold_kinds.jsonl"),
    Path("data/mlx/detect_heldout_kinds.jsonl"),
    Path("data/mlx/detect_labels.json"),
)
TEACHER_GLOB = "data/mlx/teacher/detect*.jsonl"


def psql_rows(sql: str) -> list[dict[str, Any]]:
    """docker exec psql を read-only セッションで実行し、1 行 1 JSON の結果を返す。"""
    if not sql.lstrip().lower().startswith(("select", "with")):
        raise ValueError("read-only SELECT/WITH only")
    proc = subprocess.run(  # noqa: S603 - 引数リスト・shell なし
        [  # noqa: S607
            "docker",
            "exec",
            "-i",
            "-e",
            "PGOPTIONS=-c default_transaction_read_only=on",
            "postgres",
            "psql",
            "-U",
            "kuebiko",
            "-d",
            "kuebiko",
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            "-",
        ],
        input=sql,
        capture_output=True,
        text=True,
        check=True,
    )
    return [json.loads(x) for x in proc.stdout.splitlines() if x.strip()]


def _values(items: Sequence[str]) -> str:
    return ",".join("('" + s.replace("'", "''") + "')" for s in items)


def _chunks(items: Sequence[str], n: int) -> list[Sequence[str]]:
    return [items[i : i + n] for i in range(0, len(items), n)]


def all_article_ids() -> set[str]:
    rows = psql_rows("SELECT row_to_json(t) FROM (SELECT DISTINCT article_id FROM articles) t;")
    return {str(r["article_id"]) for r in rows}


def train_side(main_root: Path, known: set[str]) -> tuple[set[str], dict[str, int]]:
    """§7.1 の学習・調整側 (DB に実在する記事 id だけ) と、出どころごとの件数。"""
    per_source: dict[str, int] = {}
    found: set[str] = set()
    files = [main_root / f for f in TRAIN_FILES] + sorted(main_root.glob(TEACHER_GLOB))
    for f in files:
        if not f.is_file():
            continue
        ids = cc.id_tokens(f.read_text(encoding="utf-8")) & known
        per_source[f.name] = len(ids)
        found |= ids
    prior: set[str] = set()
    for line in (main_root / PRIOR_ARMS).read_text(encoding="utf-8").splitlines():
        rec = json.loads(line) if line.strip() else {}
        for o in rec.get("opened", []) or []:
            prior |= {str(a) for a in o.get("article_ids", [])}
    per_source["prior_judge_units(10-27..11-04)"] = len(prior & known)
    return found | (prior & known), per_source


def entity_values() -> dict[str, dict[str, frozenset[str]]]:
    """欄 (cve / victim_org) → 記事 → 値。被害組織は照合用に正規化する。"""
    from src.assessment.evidence_verify import normalize_for_match

    rows = psql_rows(
        "SELECT row_to_json(t) FROM (SELECT article_id, entity_type, value FROM article_entities"
        " WHERE entity_type IN ('cve','victim_org')) t;"
    )
    acc: dict[str, dict[str, set[str]]] = {"cve": {}, "victim_org": {}}
    for r in rows:
        t, v = str(r["entity_type"]), str(r["value"] or "")
        val = normalize_for_match(v) if t == "victim_org" else v
        if val:
            acc[t].setdefault(str(r["article_id"]), set()).add(val)
    return {t: {a: frozenset(v) for a, v in m.items()} for t, m in acc.items()}


def event_roots() -> tuple[dict[str, str], dict[str, frozenset[str]]]:
    """(記事 → 最新の参加の根、記事 → 所属した全事象の根)。"""
    items = psql_rows("SELECT row_to_json(t) FROM (SELECT id, merged_into FROM event_items) t;")
    roots = resolve_roots({str(r["id"]): r["merged_into"] for r in items})
    members = psql_rows(
        "SELECT row_to_json(t) FROM (SELECT article_id, item_id FROM event_item_members"
        " ORDER BY joined_at) t;"
    )
    primary: dict[str, str] = {}
    every: dict[str, set[str]] = {}
    for r in members:
        a, item = str(r["article_id"]), str(r["item_id"])
        root = roots.get(item, item)
        primary[a] = root
        every.setdefault(a, set()).add(root)
    return primary, {a: frozenset(v) for a, v in every.items()}


def fetch_embeddings(ids: Sequence[str]) -> dict[str, list[float]]:
    import numpy as np

    out: dict[str, list[float]] = {}
    for ch in _chunks(sorted(ids), 4000):
        sql = f"""WITH ids(article_id) AS (VALUES {_values(ch)})
SELECT row_to_json(t) FROM (SELECT s.article_id, encode(s.vector, 'hex') AS v
 FROM summary_embeddings s JOIN ids USING (article_id) WHERE s.model = '{EMBED_MODEL}') t;"""  # noqa: S608
        for r in psql_rows(sql):
            out[str(r["article_id"])] = np.frombuffer(
                bytes.fromhex(r["v"]), dtype=np.float32
            ).tolist()
    return out


def expand(base: set[str], keys: Mapping[str, Mapping[str, frozenset[str]]]) -> set[str]:
    """base の記事が持つ鍵の値を共有する記事を足す (1 段、前回の R18.1 と同じ)。"""
    owners: dict[tuple[str, str], set[str]] = {}
    for t, m in keys.items():
        for a, vals in m.items():
            for v in vals:
                owners.setdefault((t, v), set()).add(a)
    out = set(base)
    for t, m in keys.items():
        for a in base:
            for v in m.get(a, ()):
                out |= owners[(t, v)]
    return out


def run_step(
    step: cc.ClosureStep,
    base: set[str],
    ents: Mapping[str, Mapping[str, frozenset[str]]],
    freqs: Mapping[str, Mapping[str, int]],
    roots: tuple[dict[str, str], dict[str, frozenset[str]]],
    emb_cache: dict[str, list[float]],
) -> tuple[dict[str, str], set[str]]:
    """1 段の閉包 (記事 → 成分) と母集合。"""
    keys = {t: cc.keys_for_step(ents[t], freqs[t], step) for t in ("cve", "victim_org")}
    universe = expand(base, keys)
    primary, every = roots
    root_keys = cc.keys_for_step(
        {a: every[a] for a in universe if a in every}, freqs["root"], step, per_article=False
    )
    root_of = {
        a: primary[a] for a in universe if a in primary and primary[a] in root_keys.get(a, ())
    }
    cves = {a: v for a, v in keys["cve"].items() if a in universe}
    for a, rs in root_keys.items():
        cves[a] = cves.get(a, frozenset()) | frozenset(f"ROOT:{r}" for r in rs)
    missing = sorted(a for a in universe if a not in emb_cache)
    if missing:
        emb_cache.update(fetch_embeddings(missing))
        emb_cache.update({a: [] for a in missing if a not in emb_cache})
    emb = {a: v for a, v in emb_cache.items() if a in universe and v}
    orgs = {a: v for a, v in keys["victim_org"].items() if a in universe}
    comp = build_components(
        sorted(universe),
        root_of=root_of,
        cves=cves,
        victim_orgs=orgs,
        embeddings=emb,
        cos_threshold=step.cos_threshold,
    )
    return comp, universe


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--inputs-dir", required=True)
    ap.add_argument("--main-root", default=str(ROOT), help="data/mlx を持つリポジトリの根")
    args = ap.parse_args(argv)
    in_dir, main_root = Path(args.inputs_dir), Path(args.main_root)
    inputs_path = in_dir / "inputs.json"
    inputs_bytes = inputs_path.read_bytes()
    payload = json.loads(inputs_bytes)
    windows = payload["windows"]
    window_articles = {str(a["article_id"]) for w in windows for a in w["detect_input"]}
    candidates = [str(a) for w in windows for a in w["llm_input_ids"]]
    known = all_article_ids()
    train_ids, per_source = train_side(main_root, known)
    ents = entity_values()
    roots = event_roots()
    freqs = {
        "cve": cc.value_frequencies(ents["cve"]),
        "victim_org": cc.value_frequencies(ents["victim_org"]),
        "root": cc.value_frequencies(roots[1]),
    }
    base = window_articles | train_ids
    emb_cache: dict[str, list[float]] = {}
    results: list[dict[str, Any]] = []
    shares: list[tuple[cc.ClosureStep, float]] = []
    comps: dict[str, tuple[dict[str, str], set[str]]] = {}
    for step in cc.LADDER:
        comp, universe = run_step(step, base, ents, freqs, roots, emb_cache)
        n, largest, share = component_diagnostics(comp)
        dirty = cc.contaminated_articles(comp, train_ids, window_articles)
        results.append(
            {
                "step": step.name,
                "value_freq_cap": step.value_freq_cap,
                "cos_threshold": step.cos_threshold,
                "universe": len(universe),
                "n_components": n,
                "largest_component": largest,
                "largest_component_share": share,
                "candidate_contamination": cc.share_of(dirty, candidates),
            }
        )
        print(json.dumps(results[-1], ensure_ascii=False), flush=True)
        shares.append((step, share))
        comps[step.name] = (comp, universe)  # 全段を計算して残す (§7.2)。選ぶのは最初に満たした段
    chosen = cc.choose_step(shares)
    out: dict[str, Any] = {
        "r18_version": cc.R18_2_VERSION,
        "computed_at": datetime.now(UTC).isoformat(),
        "inputs_sha256": hashlib.sha256(inputs_bytes).hexdigest(),
        "max_component_share": cc.MAX_COMPONENT_SHARE,
        "focus_cve_max": cc.FOCUS_CVE_MAX,
        "org_values_normalized": True,
        "steps": results,
        "chosen_step": chosen.name if chosen else None,
        "n_train_ids": len(train_ids),
        "train_ids_per_source": per_source,
        "n_window_articles": len(window_articles),
        "n_candidates": len(candidates),
    }
    if chosen is None:
        out["verdict"] = "undetermined: no step keeps the largest component below the cap"
    else:
        comp, universe = comps[chosen.name]
        dirty = cc.contaminated_articles(comp, train_ids, window_articles)
        cand_share = cc.share_of(dirty, candidates) or 0.0
        out.update(
            contaminated_articles=sorted(dirty),
            frozen_universe=sorted(window_articles),
            candidate_contamination=cand_share,
            per_window_candidate_contamination={
                str(w["window_id"]): cc.share_of(dirty, [str(a) for a in w["llm_input_ids"]])
                for w in windows
            },
            stop_before_gpu=cand_share > cc.CANDIDATE_CONTAMINATION_STOP,
        )
    path = in_dir / PLAN_NAME
    path.write_text(json.dumps(out, indent=2, sort_keys=True), encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    (in_dir / "closure_plan.sha256").write_text(digest + "\n", encoding="utf-8")
    summary = {
        k: v for k, v in out.items() if k not in ("contaminated_articles", "frozen_universe")
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0 if chosen is not None and not out.get("stop_before_gpu") else 1


if __name__ == "__main__":
    raise SystemExit(main())
