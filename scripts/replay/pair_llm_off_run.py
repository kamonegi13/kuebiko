"""RP-2 の対の単位 (prereg §2.1・§5.2・§6.1-6.2・§7) の実行。

DB は read-only SELECT のみ、LLM・Opus なし。

群の単位 (§5.3) は 2026-11-13 JST 以降 (prereg 変更履歴 2026-11-05 の 4)。

    uv run python -m scripts.replay.pair_llm_off_run --out <dir>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.replay import _closure as closure
from scripts.replay._gate import (
    identify_judge_set,
    load_tainted_registry,
    require_prereg,
    sha256_file,
)
from scripts.replay._manifest import Manifest, assert_no_body, write_manifest
from scripts.replay.pair_llm_off_core import (
    HARD_STRATA,
    Pair,
    arm_joins,
    mcnemar_counts,
    paired_bootstrap,
    recall_precision,
    stratum_report,
    verdict_pair_level,
)
from src.eventnews import pair_model

REPO = Path(__file__).resolve().parents[2]
MAIN = Path("/Users/juzo1192/" + "git/auto-post")
F3 = MAIN / "data/eval/f3_gold_v2.jsonl"
PAIR_INPUT = MAIN / "data/eval/pair_control/pairs_s23i.jsonl"
CONTROL = MAIN / "data/eval/pair_control/20261103_115338_pairs.jsonl"
TRAIN_FILES = tuple(MAIN / f"data/eval/eval_sample{n}.json" for n in ("", "2", "3"))
LEDGER = MAIN / "data/eval/plan_ledger/plan.yaml"
S23I_START, S23I_END = "2026-10-10T00:34:00+00:00", "2026-10-26T11:22:59+00:00"
MAX_SHARE = 0.30
FOCUS_CVE_MAX = 3
EMBED_MODEL = "snowflake-arctic-embed2"
S_MODEL = "kuebiko-sft:s24ci65"
FLAG_NAMES = ("EVENTNEWS_PAIR_LLM", "EVENT_KIND_SOURCE", "EVENTNEWS_MERGE_FOCUS_GATE")


def psql(sql: str) -> list[dict[str, Any]]:
    if not sql.lstrip().lower().startswith(("select", "with")):
        raise ValueError("read-only only")
    cmd = ["docker", "exec", "-i", "-e", "PGOPTIONS=-c default_transaction_read_only=on",
           "postgres", "psql", "-U", "kuebiko", "-d", "kuebiko", "-At", "-v", "ON_ERROR_STOP=1",
           "-f", "-"]  # fmt: skip
    out = subprocess.run(cmd, input=sql, capture_output=True, text=True, check=True)  # noqa: S603
    return [json.loads(x) for x in out.stdout.splitlines() if x.strip()]


def q(items: list[str]) -> str:
    return ",".join("('" + s.replace("'", "''") + "')" for s in items)


def chunks(items: list[str], n: int) -> list[list[str]]:
    return [items[i : i + n] for i in range(0, len(items), n)]


def read_jsonl(p: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def fetch_records(inputs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """組ごとに s23i の期間で最も早い llm_same つきの行 (両向き)。"""
    out: dict[str, dict[str, Any]] = {}
    for p in inputs:
        lid, rid = p["left_id"].replace("'", "''"), p["right_id"].replace("'", "''")
        where = (
            f"((left_id='{lid}' AND right_id='{rid}') OR (left_id='{rid}' AND right_id='{lid}'))"
            f" AND llm_same IS NOT NULL AND observed_at >= '{S23I_START}'"
            f" AND observed_at <= '{S23I_END}'"
        )
        rows = psql(
            "SELECT row_to_json(t) FROM (SELECT features_json, llm_same, observed_at"
            f" FROM event_pair_shadow WHERE {where} ORDER BY observed_at LIMIT 1) t;"  # noqa: S608
        )
        if rows:
            out[p["f3_id"]] = rows[0]
    return out


def build_pairs(
    model: pair_model.PairModel,
) -> tuple[list[Pair], dict[str, tuple[str, str]], int]:
    """(23 欄そろった組, f3_id -> 記事 id 対, 欄が足りず外した数)。"""
    f3 = {r["id"]: r for r in read_jsonl(F3) if r.get("source") == "pair_shadow"}
    inputs = read_jsonl(PAIR_INPUT)
    recs = fetch_records(inputs)
    names = model.feature_names[:-2]
    pairs: list[Pair] = []
    ids: dict[str, tuple[str, str]] = {}
    short = 0
    for p in inputs:
        rec = recs.get(p["f3_id"])
        if rec is None:
            continue
        feats = json.loads(rec["features_json"])
        if any(n not in feats for n in names):
            short += 1
            continue
        row = f3[p["f3_id"]]
        pairs.append(
            Pair(
                p["f3_id"],
                row["stratum"],
                float(row["weight"]),
                row["label"],
                tuple(float(feats[n]) for n in names),
                int(rec["llm_same"]),
            )  # fmt: skip
        )
        ids[p["f3_id"]] = (p["left_id"], p["right_id"])
    return pairs, ids, short


def train_ids() -> set[str]:
    out: set[str] = set()
    for f in TRAIN_FILES:
        for r in json.loads(f.read_text(encoding="utf-8")):
            out |= {r["a"], r["b"]}
    return out


def closure_components(base: list[str]) -> tuple[dict[str, str], int]:
    """detect_r18 と同じ作り (ハブの切り方・根・埋込)。(成分表, 閉包の記事数)。"""
    sys.path.insert(0, str(REPO / "scripts/gpu_window"))
    import detect_r18 as d  # noqa: PLC0415
    import detect_r18_core as rc  # noqa: PLC0415

    from src.assessment.evidence_verify import normalize_for_match  # noqa: PLC0415

    ents = d.fetch_entities(base)

    def cut(e: dict[str, dict[str, set[str]]]) -> dict[str, dict[str, frozenset[str]]]:
        return {t: rc.apply_hub_cut({a: frozenset(v.get(t, ())) for a, v in e.items()})
                for t in ("cve", "victim_org")}  # fmt: skip

    cuts = cut(ents)
    keys = sorted({(t, v) for t, m in cuts.items() for s in m.values() for v in s})
    universe = sorted(set(base) | d.fetch_sharing(keys))
    if len(universe) != len(base):
        ents = {**ents, **d.fetch_entities(sorted(set(universe) - set(base)))}
        cuts = cut(ents)
    primary, every = d.fetch_roots(universe)
    emb = d.fetch_embeddings(universe)
    cves = {a: v | frozenset(f"ROOT:{r}" for r in every.get(a, ())) for a, v in cuts["cve"].items()}
    for a, rs in every.items():
        cves.setdefault(a, frozenset(f"ROOT:{r}" for r in rs))
    orgs = {a: frozenset(normalize_for_match(x) for x in v if x)
            for a, v in cuts["victim_org"].items()}  # fmt: skip
    comp = closure.build_components(
        universe, root_of=primary, cves=cves, victim_orgs=orgs, embeddings=emb
    )
    return comp, len(universe)


def metrics(pairs: list[Pair], on: list[bool], off: list[bool]) -> dict[str, Any]:
    d_r, d_p = paired_bootstrap(pairs, on, off)
    r_on, p_on = recall_precision(pairs, on, weighted=True)
    r_off, p_off = recall_precision(pairs, off, weighted=True)
    return {
        "n": len(pairs), "weighted_recall_on": r_on, "weighted_recall_off": r_off,
        "weighted_precision_on": p_on, "weighted_precision_off": p_off,
        "delta_recall": d_r.__dict__,
        "delta_precision": d_p.__dict__, "verdict": verdict_pair_level(d_r, d_p),
        "mcnemar": mcnemar_counts(pairs, on, off),
        "hard_strata_unweighted": stratum_report(pairs, on, off, HARD_STRATA),
        "unweighted_recall_on": recall_precision(pairs, on, weighted=False)[0],
        "unweighted_recall_off": recall_precision(pairs, off, weighted=False)[0],
    }  # fmt: skip


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    out = Path(args.out)
    started = datetime.now(UTC)
    rec = require_prereg("pair_llm_off", REPO, run_started_at=started,
                         caller=Path(__file__), out_dir=out)  # fmt: skip
    registry = load_tainted_registry(LEDGER, MAIN)
    judge = identify_judge_set(F3, registry, MAIN)
    model_path = REPO / "config/models/pair_model.json"
    model = pair_model.load_model(str(model_path))
    if model is None:
        raise SystemExit("pair_model not loadable")
    pairs, ids, short = build_pairs(model)
    train = train_ids()
    comp, universe = closure_components(sorted({a for ab in ids.values() for a in ab} | train))
    share = closure.component_diagnostics(comp)
    train_comps = {comp.get(a, a) for a in train}
    contaminated = {
        p.f3_id for p in pairs if any(comp.get(a, a) in train_comps for a in ids[p.f3_id])
    }
    main_pairs = [p for p in pairs if p.f3_id not in contaminated]
    held = [p for p in pairs if p.f3_id in contaminated]
    result: dict[str, Any] = {"main": None, "r18_excluded_layer": None}
    on = arm_joins(model, main_pairs, llm_off=False)
    off = arm_joins(model, main_pairs, llm_off=True)
    result["main"] = metrics(main_pairs, on, off)
    if held:
        result["r18_excluded_layer"] = metrics(
            held, arm_joins(model, held, llm_off=False), arm_joins(model, held, llm_off=True)
        )
    # 6.4 の代わりの案 (S の llm_same を LLM ありの腕に、記録の s23i を基準に): 参考として併記
    ctrl = {frozenset((r["left_id"], r["right_id"])): r["llm_same"] for r in read_jsonl(CONTROL)}
    by_f3 = {f: ctrl.get(frozenset(ab)) for f, ab in ids.items()}
    if all(by_f3.get(p.f3_id) for p in main_pairs):
        s_same = {f: int(v[S_MODEL]) for f, v in by_f3.items() if v}
        result["alternative_S"] = metrics(
            main_pairs, on, arm_joins(model, main_pairs, llm_off=False, llm_same=s_same)
        )
    agree = [
        bool(by_f3[p.f3_id]["kuebiko-sft:s23i"]) == bool(p.llm_same)
        for p in pairs
        if by_f3.get(p.f3_id)
    ]
    result["s23i_reproducibility_agreement"] = sum(agree) / len(agree) if agree else None
    result["counts"] = {
        "pairs_total": len(pairs), "excluded_short_features": short,
        "excluded_r18": len(held), "main": len(main_pairs),
        "labels_main": dict(Counter(p.label for p in main_pairs)),
        "strata_main": dict(Counter(p.stratum for p in main_pairs)),
    }  # fmt: skip
    result["r18"] = {"version": closure.R18_VERSION, "universe": universe,
                     "n_components": share[0], "largest": share[1],
                     "largest_share": share[2], "max_share": MAX_SHARE,
                     "share_breached": share[2] > MAX_SHARE}  # fmt: skip
    assert_no_body(result)
    out.mkdir(parents=True, exist_ok=True)
    res_path = out / "pair_level_result.json"
    res_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    tiers = psql("SELECT row_to_json(t) FROM (SELECT MAX(version) AS v FROM app_config_versions"
                 " WHERE config_key='model_tiers') t;")  # fmt: skip
    write_manifest(
        Manifest(
            name="pair_llm_off:pair_level",
            period_start=S23I_START,
            period_end=S23I_END,
            split_cutoff="none (no tuning in RP-2)",
            input_kinds={
                "features_json": "recorded",
                "llm_same": "recorded(s23i)",
                "closure": "current DB (read-only)",
            },
            head_sha=rec.head_sha,
            prereg_path=rec.path,
            prereg_git_sha=rec.git_sha,
            prereg_blob_sha256=rec.blob_sha256,
            judge_sets=[{"path": judge.path, "sha256": judge.sha256, "rows": judge.rows}],
            model_tiers_version=tiers[0]["v"],
            r18_version=closure.R18_VERSION,
            flags=dict.fromkeys(FLAG_NAMES, "(unset in this process)")
            | {
                "pair_model_sha256": sha256_file(model_path),
                "result_sha256": hashlib.sha256(res_path.read_bytes()).hexdigest(),
            },
            max_component_share=MAX_SHARE,
            component_share=share[2],
            n_components=share[0],
            counts={"main": len(main_pairs), "excluded_r18": len(held), "pairs_total": len(pairs)},
        ),
        out,
    )
    print(json.dumps({k: result[k] for k in ("counts", "r18")}, ensure_ascii=False))
    print(json.dumps(result["main"]["verdict"]), result["main"]["delta_recall"],
          result["main"]["delta_precision"])  # fmt: skip
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
