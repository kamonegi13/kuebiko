#!/usr/bin/env python3
"""DETECT-JUDGE の Opus 裁定と採点 (GPU-B の 2 腕の出力を対にして合否を出す)。

仕様は docs/replay_prereg/detect_decompose.md (§5.2・§5.3・§6 と変更履歴)。本番 DB は触らない。
``DETECT_GPU_PROFILE=rejudge`` なら docs/replay_prereg/detect_decompose_rejudge.md
(乱数種 20261105・
入力 data/eval/detect_rejudge/・出力 judge/・閉包は closure_plan.json の汚染記事で読む)。

    python scripts/gpu_window/detect_judge.py plan         # 単位・R18 の直接検査・抽出 (Opus なし)
    python scripts/gpu_window/detect_judge.py adjudicate   # Opus 裁定 (キャッシュで再開できる)
    python scripts/gpu_window/detect_judge.py score        # 指標と合否 (Opus なし)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))
import detect_judge_core as jc  # noqa: E402

MAIN_ROOT = Path("/Users/juzo1192/git/auto-post")
IS_REJUDGE = jc.PROFILE_NAME == "rejudge"
LEDGER = MAIN_ROOT / "data" / "eval" / "adjudication_ledger.jsonl"
GOLDSET = MAIN_ROOT / "data" / "mlx" / "detect_goldset.jsonl"
TEACHER_GLOB = "detect*.jsonl"
if IS_REJUDGE:
    # docs/replay_prereg/detect_decompose_rejudge.md (§4・§7.2・§8・§11)
    PREREG_PATH = "docs/replay_prereg/detect_decompose_rejudge.md"
    EVAL_DIR = MAIN_ROOT / "data" / "eval" / "detect_rejudge"
    ARMS_NAME = ""  # arms_*.jsonl のうち最新 (--arms で指定もできる)
    JUDGE_DIR = EVAL_DIR / "judge"
    CLOSURE_PATH = EVAL_DIR / "closure_plan.json"
    ITEM = "DETECT-REJUDGE"
    RUBRIC = "detect-rejudge-v1"
    RUBRIC_P2 = "detect-rejudge-v1-p2"
    GOLD_ID = "gold:detect_rejudge_past"
    # 判定にしてはならない集合 (前回の裁定、§8)
    FORBIDDEN_JUDGE_DIRS = (
        MAIN_ROOT / "data" / "eval" / "detect_decompose" / "judge",
        MAIN_ROOT / "data" / "eval" / "detect_decompose" / "judge_r18",
    )
else:
    PREREG_PATH = "docs/replay_prereg/detect_decompose.md"
    EVAL_DIR = MAIN_ROOT / "data" / "eval" / "detect_decompose"
    ARMS_NAME = "arms_20261104T022916Z.jsonl"
    JUDGE_DIR = EVAL_DIR / "judge_r18"
    CLOSURE_PATH = JUDGE_DIR / "closure.json"
    ITEM = "DETECT-JUDGE-R18"
    RUBRIC = "detect-judge-v1"
    RUBRIC_P2 = "detect-judge-v1-p2"
    GOLD_ID = "gold:detect_fresh_1027"
    FORBIDDEN_JUDGE_DIRS = ()
OLD_JUDGE_DIR = EVAL_DIR / "judge"  # decompose: 11-04 の簡略 R18 の裁定 (参考、消さない)
BUDGET_ESTIMATE = 300
CALL_CAP = 390  # 見積もりの 1.3 倍
CONCURRENCY = 2

QUESTION = (
    "次の『開設』は、窓の開始前から追跡中の情勢に対し新しく追跡を始める claim 1 文と、その根拠の記事です。"  # noqa: E501
    "3 つの問いに答えてください。事象の同一性は次の定義に従います: 同じ事案の別媒体・別言語の報道と第 n 報は"  # noqa: E501
    "同じ事象。修正の告知とその悪用、当局の処分・指導など派生、同じ行為者の別の作戦、まとめ記事は別の事象。\n"
    "q1_new_event: claim は追跡中の情勢のどれとも『同じ事象でない』か (どれかと同じ事案の続報なら no)。\n"  # noqa: E501
    "q2_trackable: 追跡する価値があるか (続報で見立てが動きうるなら yes。制裁発表・解体・統計・"
    "衛星画像分析のような単発で終わるものは no)。\n"
    "q3_faithful: claim は記事の見出し・要約の範囲内か (見出し・要約に無い属性・格上げ・複数文を足していなければ yes)。\n"  # noqa: E501
    "各々 yes / no / undecidable のどれか。reason は 80 字以内。"
)


class DetectVerdict(BaseModel):
    q1_new_event: Literal["yes", "no", "undecidable"]
    q2_trackable: Literal["yes", "no", "undecidable"]
    q3_faithful: Literal["yes", "no", "undecidable"]
    reason: str = Field(default="", max_length=400)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_inputs() -> tuple[
    list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]
]:
    data = json.loads((EVAL_DIR / "inputs.json").read_text(encoding="utf-8"))
    windows = {w["window_id"]: w for w in data["windows"]}
    return data["windows"], windows, data


def article_view(window: Mapping[str, Any], aid: str) -> dict[str, str]:
    """裁定に渡す記事: 見出し・要約・公開日 (窓の終わりまで)。本文は渡さない。"""
    art = window["articles"][aid]
    titles = {d["article_id"]: d.get("title") for d in window.get("detect_input", [])}
    title = titles.get(aid) or str(art.get("body_prefix", "")).split("\n", 1)[0]
    return {
        "title": str(title),
        "summary": str(art.get("summary", "")),
        "published": str(art.get("created_at", "")),
    }


def build_inputs(window: Mapping[str, Any], unit: jc.Unit) -> dict[str, Any]:
    """腕・モデル・確率・run 番号・他の単位は渡さない (盲検)。"""
    return {
        "claim": unit.claim,
        "articles": [article_view(window, a) for a in unit.article_ids],
        "tracked_titles_before_window": list(window.get("active_titles", [])),
    }


ARMS_OVERRIDE: dict[str, str] = {}


def arms_path(plan: Mapping[str, Any] | None = None) -> Path:
    """2 腕の出力: --arms、plan の記録、なければ (rejudge) 最新の arms_*.jsonl。"""
    name = ARMS_OVERRIDE.get("name") or (plan or {}).get("arms_name") or ARMS_NAME
    if name:
        return EVAL_DIR / str(name)
    found = sorted(EVAL_DIR.glob("arms_*.jsonl"))
    if not found:
        raise SystemExit(f"{EVAL_DIR} に arms_*.jsonl が無い (--arms で指定)")
    return found[-1]


def load_closure_excluded(
    units: Mapping[str, jc.Unit],
) -> tuple[set[str], dict[str, Any]]:
    """除く単位と閉包の記録。

    decompose: detect_r18.py が書いた closure.json (学習側と同じ成分に入る単位)。
    rejudge: 凍結時に固定した closure_plan.json の汚染記事の集合で読む (R18 を計算し直さない)。
    """
    closure = json.loads(CLOSURE_PATH.read_text(encoding="utf-8"))
    if not IS_REJUDGE:
        return set(closure["excluded"]), closure
    if closure.get("chosen_step") is None:
        raise SystemExit("closure_plan.json に選んだ段が無い (判定不能、裁定に進まない)")
    excluded = jc.excluded_by_closure_plan(
        units,
        frozenset(closure["contaminated_articles"]),
        frozenset(closure["frozen_universe"]),
    )
    closure = {**closure, "r18_version": closure.get("r18_version", "R18.2")}
    return excluded, closure


def check_not_forbidden() -> None:
    """§8: 出力先が前回の裁定の置き場と同じでないこと。"""
    for bad in FORBIDDEN_JUDGE_DIRS:
        if JUDGE_DIR.resolve() == bad.resolve():
            raise SystemExit(f"出力先が前回の裁定 {bad} と同じ")


def excluded_layer(units: Mapping[str, jc.Unit], excluded: set[str]) -> dict[str, Any]:
    """除いた単位のうち 11-04 の裁定 (参考) があるものの内訳 (別の層)。"""
    old = load_labels(OLD_JUDGE_DIR / "verdicts.jsonl")
    out: dict[str, dict[str, int]] = {}
    for uid in sorted(excluded & set(old)):
        for arm in units[uid].arms:
            c = out.setdefault(arm, {jc.CORRECT: 0, jc.WRONG: 0, jc.UNDECIDED: 0})
            c[old[uid]["label"]] += 1
    return {"n_with_old_verdict": len(excluded & set(old)), "label_counts_by_arm": out}


def jc_count(units: Mapping[str, jc.Unit], uids: set[str]) -> dict[str, int]:
    import detect_r18_core as rc

    return rc.count_by_stratum(units, uids)


def cmd_plan(_: argparse.Namespace) -> int:
    check_not_forbidden()
    arms_file = arms_path()
    records = read_jsonl(arms_file)
    units = jc.collect_units(records)
    excluded, closure = load_closure_excluded(units)
    kept = {k: v for k, v in units.items() if k not in excluded}
    sampled, weights = jc.sample_units(kept)
    second = jc.pick_second(sampled)
    JUDGE_DIR.mkdir(parents=True, exist_ok=True)
    plan = {
        "n_units_total": len(units),
        "n_r18_excluded": len(excluded),
        "r18_excluded_by_stratum": jc_count(units, excluded),
        "r18_version": closure["r18_version"],
        "r18_excluded_layer": excluded_layer(units, excluded) if not IS_REJUDGE else {},
        "closure_sha256": sha256_file(CLOSURE_PATH),
        "n_units_kept": len(kept),
        "strata": {s: sum(1 for u in kept.values() if u.stratum == s) for s in sorted(weights)},
        "weights": weights,
        "sampled": jc.shuffled_order(sampled),
        "second": second,
        "excluded_r18": sorted(excluded),
        "arms_sha256": sha256_file(arms_file),
        "inputs_sha256": sha256_file(EVAL_DIR / "inputs.json"),
        "seed": jc.SEED,
        **(
            {
                "arms_name": arms_file.name,
                "profile": jc.PROFILE_NAME,
                "closure_chosen_step": closure.get("chosen_step"),
            }
            if IS_REJUDGE
            else {}
        ),
    }
    (JUDGE_DIR / "plan.json").write_text(
        json.dumps(plan, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(
        json.dumps(
            {k: v for k, v in plan.items() if k not in ("sampled", "second", "excluded_r18")},
            ensure_ascii=False,
        )
    )
    return 0


def _cached(key: str) -> bool:
    import adjudication_cache as ac

    return ac._state.get_cached(key, ac.CACHE_PATH) is not None


class CallBudget:
    """Opus の実呼び出し (キャッシュ当たりを除く) を数え、上限で止める。"""

    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.charged = 0
        self.cache_hits = 0
        self.failed = 0

    def can_call(self) -> bool:
        return self.charged < self.cap


async def judge_all(
    todo: Sequence[str],
    units: Mapping[str, jc.Unit],
    windows: Mapping[str, Mapping[str, Any]],
    rubric: str,
    budget: CallBudget,
) -> dict[str, DetectVerdict | None]:
    import adjudication_cache as ac
    from b5_opus_confirm import make_client

    client = make_client()
    sem = asyncio.Semaphore(CONCURRENCY)
    out: dict[str, DetectVerdict | None] = {}

    async def one(uid: str) -> None:
        u = units[uid]
        inputs = build_inputs(windows[u.window_id], u)
        key = ac.make_key(
            QUESTION, inputs, DetectVerdict, rubric=rubric, judge="claude-opus-5-5", effort="medium"
        )
        async with sem:
            if _cached(key):
                budget.cache_hits += 1
            elif not budget.can_call():
                return
            else:
                budget.charged += 1
            v = await ac.adjudicate(QUESTION, inputs, DetectVerdict, rubric=rubric, client=client)
        if v is None:
            budget.failed += 1
        out[uid] = v

    await asyncio.gather(*(one(u) for u in todo))
    return out


def write_verdicts(path: Path, rows: Mapping[str, DetectVerdict | None]) -> None:
    lines = []
    for uid in sorted(rows):
        v = rows[uid]
        if v is None:
            continue
        lines.append(
            json.dumps(
                {
                    "unit_id": uid,
                    **v.model_dump(),
                    "label": jc.combine(v.q1_new_event, v.q2_trackable, v.q3_faithful),
                },
                ensure_ascii=False,
            )
        )
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def append_ledger(budget: CallBudget, n_done: int) -> None:
    row = {
        "time": datetime.now(UTC).isoformat(),
        "item": ITEM,
        "judge": "claude-opus-5-5",
        "rubric": RUBRIC,
        "opus_calls_charged": budget.charged,
        "budget_limit": BUDGET_ESTIMATE,
        "call_cap": CALL_CAP,
        "n_done": n_done,
        "n_failed": budget.failed,
    }
    with LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def cmd_adjudicate(_: argparse.Namespace) -> int:
    sys.path.insert(0, str(MAIN_ROOT / "data" / "mlx"))
    sys.path.insert(0, str(MAIN_ROOT))
    import adjudication_cache as ac

    plan = json.loads((JUDGE_DIR / "plan.json").read_text(encoding="utf-8"))
    records = read_jsonl(arms_path(plan))
    units = jc.collect_units(records)
    _wl, windows, _raw = load_inputs()
    budget = CallBudget(CALL_CAP)
    p1: dict[str, DetectVerdict | None] = {}
    p2: dict[str, DetectVerdict | None] = {}
    status = 0
    try:
        for _round in range(2):  # 失敗は 1 回だけ再試行 (上限の内側で)
            todo1 = [u for u in plan["sampled"] if p1.get(u) is None]
            p1.update(asyncio.run(judge_all(todo1, units, windows, RUBRIC, budget)))
        for _round in range(2):
            todo2 = [u for u in plan["second"] if p2.get(u) is None]
            p2.update(asyncio.run(judge_all(todo2, units, windows, RUBRIC_P2, budget)))
    except ac.LimitReached as exc:
        print(f"LimitReached: {exc} (キャッシュから再開できる)")
        status = 2
    write_verdicts(JUDGE_DIR / "verdicts.jsonl", p1)
    write_verdicts(JUDGE_DIR / "verdicts_pass2.jsonl", p2)
    append_ledger(
        budget, sum(v is not None for v in p1.values()) + sum(v is not None for v in p2.values())
    )
    print(
        json.dumps(
            {
                "charged": budget.charged,
                "cache_hits": budget.cache_hits,
                "failed": budget.failed,
                "pass1": sum(v is not None for v in p1.values()),
                "pass2": sum(v is not None for v in p2.values()),
            }
        )
    )
    return status


def load_labels(path: Path) -> dict[str, dict[str, Any]]:
    return {r["unit_id"]: r for r in read_jsonl(path)} if path.is_file() else {}


def cmd_score(_: argparse.Namespace) -> int:
    plan = json.loads((JUDGE_DIR / "plan.json").read_text(encoding="utf-8"))
    records = read_jsonl(arms_path(plan))
    units = jc.collect_units(records)
    p1 = load_labels(JUDGE_DIR / "verdicts.jsonl")
    p2 = load_labels(JUDGE_DIR / "verdicts_pass2.jsonl")
    labels = {u: r["label"] for u, r in p1.items()}
    sampled = set(plan["sampled"])
    rates = jc.arm_rates(units, plan["sampled"], labels)
    values = jc.window_arm_values(records, units, labels, sampled, rates, set(plan["excluded_r18"]))
    stats = jc.analyze(values)
    determinable = sum(1 for u in plan["sampled"] if labels.get(u) in (jc.CORRECT, jc.WRONG))
    share = determinable / len(plan["sampled"]) if plan["sampled"] else 0.0
    p3, p4, p5 = jc.p3_share(records), jc.p4_share(records), jc.p5_ratio(records)
    decision = (
        jc.decide(
            stats,
            p3,
            p4,
            p5,
            share,
            excluded_share=(
                plan["n_r18_excluded"] / plan["n_units_total"]
                if IS_REJUDGE and plan["n_units_total"]
                else None
            ),
        )
        if stats.get("n_windows")
        else {"verdict": "undetermined"}
    )
    ship_units = [u for u in plan["sampled"] if "ship" in units[u].arms and u in p1]
    q3_no = sum(1 for u in ship_units if p1[u]["q3_faithful"] == "no")
    both = [u for u in plan["second"] if u in p1 and u in p2]
    agree = [
        jc.combine(p1[u]["q1_new_event"], p1[u]["q2_trackable"], p1[u]["q3_faithful"]) for u in both
    ]
    agree2 = [
        jc.combine(p2[u]["q1_new_event"], p2[u]["q2_trackable"], p2[u]["q3_faithful"]) for u in both
    ]
    summary = {
        "decision": decision,
        "stats": stats,
        "p3": {
            **p3,
            "q3_no_share_ship_sampled": q3_no / len(ship_units) if ship_units else None,
            "q3_no": q3_no,
            "n_ship_sampled_judged": len(ship_units),
        },
        "p4": p4,
        "p5": p5,
        "determinable_share_of_sampled": share,
        "n_sampled": len(plan["sampled"]),
        "n_judged": len(p1),
        "n_units_total": plan["n_units_total"],
        "n_r18_excluded": plan["n_r18_excluded"],
        "r18_excluded_by_stratum": plan["r18_excluded_by_stratum"],
        "r18_excluded_layer": plan["r18_excluded_layer"],
        "r18_version": plan["r18_version"],
        "arm_rates_correct_wrong": {a: list(r) for a, r in rates.items()},
        "second_pass": {
            "n": len(both),
            "agreement": sum(a == b for a, b in zip(agree, agree2, strict=True)) / len(both)
            if both
            else None,
            "kappa": jc.cohen_kappa(agree, agree2),
        },
        "label_counts": {
            a: {
                lab: sum(1 for u in plan["sampled"] if a in units[u].arms and labels.get(u) == lab)
                for lab in (jc.CORRECT, jc.WRONG, jc.UNDECIDED)
            }
            for a in ("current", "ship")
        },
    }
    (JUDGE_DIR / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    manifest = {
        "prereg_path": PREREG_PATH,
        "arms_sha256": plan["arms_sha256"],
        "inputs_sha256": plan["inputs_sha256"],
        "plan_sha256": sha256_file(JUDGE_DIR / "plan.json"),
        "verdicts_sha256": sha256_file(JUDGE_DIR / "verdicts.jsonl") if p1 else None,
        "verdicts_pass2_sha256": sha256_file(JUDGE_DIR / "verdicts_pass2.jsonl") if p2 else None,
        "summary_sha256": sha256_file(JUDGE_DIR / "summary.json"),
        "judge": "claude-opus-5-5",
        "rubric": [RUBRIC, RUBRIC_P2],
        "seed": jc.SEED,
        "scored_at": datetime.now(UTC).isoformat(),
        "gold_id": GOLD_ID,
        "r18_version": plan["r18_version"],
        "closure_sha256": plan["closure_sha256"],
    }
    (JUDGE_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=1, sort_keys=True)[:6000])
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("plan", cmd_plan), ("adjudicate", cmd_adjudicate), ("score", cmd_score)):
        sp = sub.add_parser(name)
        sp.add_argument("--arms", default="", help="arms_*.jsonl のファイル名 (plan のとき)")
        sp.set_defaults(fn=fn)
    args = ap.parse_args(argv)
    if args.arms:
        ARMS_OVERRIDE["name"] = args.arms
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
