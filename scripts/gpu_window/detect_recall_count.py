#!/usr/bin/env python3
"""DETECT-RECALL の効きを LLM なし・DB なしで数える (凍結入力と腕の出力だけを読む)。

    detect_recall_count.py merge-cap [--eval-dir data/eval/detect_rejudge]
    detect_recall_count.py salvage   [--eval-dir data/eval/detect_decompose]

``merge-cap``: 凍結入力 (inputs.json) と出荷腕の出力 (arms_*.jsonl の最新) から、塊化・合流・
書き手の上限の挙動を決定論で再構成する (旗 0 の再構成が腕の記録と一致することを先に示す)。旗 1 で
(a) 合流の関門 (``DETECT_MERGE_CONFLICT_GATE``) が止める合流の塊数と型、
(b) 書き手の上限の補充 (``DETECT_WRITER_CAP_REFILL``) で書き手を呼べる塊の数、を数える。
書き手の出力は腕の記録を使い、記録に無い塊 (合流を止めて新たに書き手へ回る・上限の外だった塊) の
結果は**開設しない側**に置く (= 呼出の数は正しく、開設の増分は上限)。被害組織の別名判定 (DB) は
読まないので、組織名の食い違いの止まり方は別名を知らない分だけ多めに出る。

``salvage``: 忠実さの関門を付けずに走らせた腕 (11-04 の判定) の出荷腕の claim に関門を当て、止まる
claim に ``DETECT_CLAIM_SALVAGE`` の削りを当てて、通る件数を数える (LLM の出力は使わない)。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

DEFAULT_MERGE_DIR = Path("data/eval/detect_rejudge")
DEFAULT_SALVAGE_DIR = Path("data/eval/detect_decompose")
SHIP_ARM = "ship"


def latest_arms(eval_dir: Path) -> Path:
    files = sorted(eval_dir.glob("arms_*.jsonl"))
    if not files:
        raise SystemExit(f"{eval_dir} に arms_*.jsonl が無い")
    return files[-1]


def load_inputs(eval_dir: Path) -> dict[str, Any]:
    from detect_common import verify_inputs  # noqa: PLC0415

    verify_inputs(eval_dir)
    return dict(json.loads((eval_dir / "inputs.json").read_text(encoding="utf-8")))


def ship_rows(arms_path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(x) for x in arms_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    return [r for r in rows if r.get("arm") == SHIP_ARM and r.get("ok")]


# ---- merge-cap -------------------------------------------------------------------------------


def _window_parts(w: Mapping[str, Any]) -> dict[str, Any]:
    from detect_common import FrozenRepo, merge_scores_from_frozen  # noqa: PLC0415

    from src.assessment.assignment import split_anchor_keys  # noqa: PLC0415
    from src.assessment.stateful import _unit_vectors  # noqa: PLC0415

    repo = FrozenRepo(w)
    ids = [str(a) for a in w["llm_input_ids"]]
    entities = {a: set(v) for a, v in w["entities"].items()}
    scores, sids = merge_scores_from_frozen(w)
    arts = w["articles"]
    titles = {a["article_id"]: a["title"] for a in w["detect_input"]}
    return {
        "titles": {a: titles.get(a, "") for a in ids},
        "summaries": {a: arts[a].get("summary") or "" for a in arts},
        "bodies": {a: arts[a].get("body_prefix") or "" for a in arts},
        "feeds": {a: arts[a].get("feed_title") or "" for a in arts},
        "candidate_ids": ids,
        "ml_scores": {a: float(w["ml_scores"].get(a, 0.0)) for a in ids},
        "strong_by_aid": {a: split_anchor_keys(frozenset(entities.get(a, set())))[0] for a in ids},
        "items_by_aid": repo.event_item_ids_by_article(ids),
        "vec_by_aid": _unit_vectors(repo.load_summary_embeddings(ids)),
        "entities": entities,
        "scores": scores,
        "sids": sids,
        "situations": {s["situation_id"]: s for s in w["situations"]},
    }


async def _simulate(
    parts: Mapping[str, Any], opened: frozenset[frozenset[str]], *, gate: Any
) -> Any:
    from src.assessment.detect_decompose import (  # noqa: PLC0415
        WrittenClaim,
        _grounding_sources,
        decompose_candidates,
        pick_merge,
    )

    by_id = {a: {"title": t} for a, t in parts["titles"].items()}

    blocked: list[tuple[str, ...]] = []

    def merge_for(cluster: Sequence[str]) -> Any:
        verdict = pick_merge(cluster, parts["scores"], parts["sids"])
        if gate is None or verdict is None or gate.conflict(cluster, verdict.situation_id) is None:
            return verdict
        blocked.append(tuple(cluster))
        return pick_merge(cluster, parts["scores"], gate.allowed(cluster, parts["sids"]))

    async def write(cluster: Sequence[str]) -> WrittenClaim | None:
        if frozenset(cluster) in opened:
            return WrittenClaim("x", "cyber_incident", "")
        return None  # 記録に無い塊は開設しない側 (呼出だけ消費する)

    res = await decompose_candidates(
        candidate_ids=parts["candidate_ids"],
        ml_scores=parts["ml_scores"],
        strong_by_aid=parts["strong_by_aid"],
        items_by_aid=parts["items_by_aid"],
        vec_by_aid=parts["vec_by_aid"],
        merge_for=merge_for,
        write_claim=write,
        grounding_text=lambda c: _grounding_sources(
            by_id, parts["summaries"], parts["bodies"], parts["feeds"], c
        ),
        enforce_grounding=True,
    )
    return res, blocked


def _summ(res: Any) -> dict[str, int]:
    one_off = sum(1 for o in res.outcomes if o.reason.startswith("単発の事案"))
    calls = len(res.outcomes) - len(res.merged()) - res.overflow - one_off
    return {
        "clusters": len(res.outcomes),
        "merged_clusters": len(res.merged()),
        "merged_articles": sum(len(o.cluster) for o in res.merged()),
        "writer_calls": calls,
        "overflow": res.overflow,
    }


def _written(res: Any) -> set[frozenset[str]]:
    return {
        frozenset(o.cluster)
        for o in res.outcomes
        if o.action != "merge" and o.reason != "開設の上限"
    }


async def count_merge_cap(eval_dir: Path) -> dict[str, Any]:
    import os  # noqa: PLC0415

    os.environ["DETECT_WRITER_GATES"] = "1"  # 出荷腕の設定 (単発の事案は書き手を呼ばない)
    from src.assessment.detect_merge_gate import MergeConflictGate  # noqa: PLC0415

    inputs = load_inputs(eval_dir)
    windows = {w["window_id"]: w for w in inputs["windows"]}
    parts_by_window = {wid: _window_parts(w) for wid, w in windows.items()}
    rows = ship_rows(latest_arms(eval_dir))
    tot: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    match: Counter[str] = Counter()
    for r in rows:
        parts = parts_by_window[r["window_id"]]
        opened = frozenset(frozenset(o["article_ids"]) for o in r["opened"])
        gate = MergeConflictGate(
            entities_by_id=parts["entities"],
            anchors_by_sid={k: frozenset(v["anchors"]) for k, v in parts["situations"].items()},
            titles_by_sid={k: v["title"] for k, v in parts["situations"].items()},
        )
        os.environ.pop("DETECT_WRITER_CAP_REFILL", None)
        base, _ = await _simulate(parts, opened, gate=None)
        gated, blocked = await _simulate(parts, opened, gate=gate)
        os.environ["DETECT_WRITER_CAP_REFILL"] = "1"
        refill, _ = await _simulate(parts, opened, gate=None)
        both, _ = await _simulate(parts, opened, gate=gate)
        os.environ.pop("DETECT_WRITER_CAP_REFILL", None)
        b = _summ(base)
        match["rows"] += 1
        match["merged_articles_eq"] += b["merged_articles"] == r["n_merged_articles"]
        match["overflow_eq"] += b["overflow"] == r["overflow"]
        for name, res in (("base", base), ("gate", gated), ("refill", refill), ("both", both)):
            for k, v in _summ(res).items():
                tot[f"{name}.{k}"] += v
        tot["gate.blocked_clusters"] += len(blocked)
        for cluster in blocked:
            sid = next((s for s in parts["sids"] if gate.conflict(cluster, s) is not None), None)
            kinds[str(gate.conflict(cluster, sid)) if sid else "?"] += 1
        tot["refill.new_written"] += len(_written(refill) - _written(base))
        tot["both.new_written_vs_base"] += len(_written(both) - _written(base))
    return {
        "ship_rows": len(rows),
        "reconstruction_matches_recorded": dict(match),
        "totals": dict(sorted(tot.items())),
        "blocked_kinds": dict(kinds),
    }


# ---- salvage ---------------------------------------------------------------------------------


def count_salvage(eval_dir: Path) -> dict[str, Any]:
    from src.assessment.claim_fidelity import check_fidelity  # noqa: PLC0415
    from src.assessment.claim_salvage import salvage_claim  # noqa: PLC0415
    from src.assessment.detect_decompose import _grounding_sources  # noqa: PLC0415

    inputs = load_inputs(eval_dir)
    windows = {w["window_id"]: w for w in inputs["windows"]}
    total = blocked = passed = 0
    removed_kinds: Counter[tuple[str, ...]] = Counter()
    flag_kinds: Counter[str] = Counter()
    for r in ship_rows(latest_arms(eval_dir)):
        w = windows[r["window_id"]]
        titles = {a["article_id"]: a["title"] for a in w["detect_input"]}
        arts = w["articles"]
        by_id = {a: {"title": titles.get(a, "")} for a in arts}
        summaries = {a: v.get("summary") or "" for a, v in arts.items()}
        bodies = {a: v.get("body_prefix") or "" for a, v in arts.items()}
        feeds = {a: v.get("feed_title") or "" for a, v in arts.items()}
        for o in r["opened"]:
            total += 1
            src = _grounding_sources(by_id, summaries, bodies, feeds, o["article_ids"])
            flags = check_fidelity(o["claim"], src)
            if not flags:
                continue
            blocked += 1
            flag_kinds.update({f.split(":")[0] for f in flags})
            got = salvage_claim(o["claim"], src)
            if got is not None:
                passed += 1
                removed_kinds[got.removed] += 1
    return {
        "ship_claims": total,
        "blocked_by_fidelity": blocked,
        "salvaged_and_passed": passed,
        "flag_kinds_in_blocked": dict(flag_kinds),
        "removed_kinds": {",".join(k): v for k, v in removed_kinds.items()},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("what", choices=("merge-cap", "salvage"))
    ap.add_argument("--eval-dir", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None, help="結果の JSON の書き出し先")
    args = ap.parse_args()
    if args.what == "merge-cap":
        out = asyncio.run(count_merge_cap(args.eval_dir or DEFAULT_MERGE_DIR))
    else:
        out = count_salvage(args.eval_dir or DEFAULT_SALVAGE_DIR)
    text = json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
