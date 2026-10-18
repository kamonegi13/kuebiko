#!/usr/bin/env python3
"""logprob 判定 4 本のしきい値を、モデルごとに較正する (s24_plan §4.2、2026-10-18)。

対象: thin_body_judge / pir_focus_claim_judge / assign_evidence_judge / org_identity_judge。
モデルは**引数で受ける** (切替前の候補を測れる。``resolve_step_model`` の解決には頼らない)。
問いの描画と確率の読み出しは本番の関数 (``decision_probe.probe_choice`` ほか) をそのまま呼ぶ。

    count                        較正集の件数と判定呼び出し数を数える (GPU なし)
    build                        較正集を凍結する (GPU なし)
                                 → data/eval/probe_calib/<step>.jsonl + manifest.json
    run   --model M              較正集を流す (GPU)。再開可能 → data/eval/probe_calib/runs/<M>.jsonl
    fit   --model M [--write]    runs から AUC・ECE・Platt・確信帯・しきい値・分割外の動作点を出す
                                 (GPU なし)。--write で config/models/probe_thresholds.json に
                                 M の項を足す (他のモデルの項は消さない)
    gate  --model M --baseline B 合格線 (s24_plan §4.2) を M と B の report で判定する (GPU なし)
    check                        model_tiers が解決する全モデルに 4 本の項があるか調べる (GPU なし)

切り替え手順 (毎回この順): ①probe_thresholds.json に今のモデルと戻し先の項があること (check) →
②移行先を --model にして run → fit → gate → ③合格した判定だけ UI の step 上書きを変える →
④全 step の実物確認。**1 本でも満たさない判定は旧モデルのまま**。

失敗行は書かない (再実行で再試行)。モデル名は CLAUDE.md §4 の denylist を通す。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.tools.probe_calibration import (  # noqa: E402
    Operating,
    OrgRow,
    Row,
    confident_band_accuracy,
    ece,
    operating_high_accept,
    operating_low_flag,
    org_same,
    out_of_fold_high_accept,
    out_of_fold_low_flag,
    out_of_fold_org,
    pick_high_accept_threshold,
    pick_low_flag_threshold,
    pick_org_thresholds,
    platt_apply,
    platt_fit,
    weighted_auc,
)
from src.tools.probe_thresholds import (  # noqa: E402
    CURRENT_PRODUCTION_CONSTANTS,
    MAIN_QUESTION,
    PROBE_STEP_QUESTIONS,
    THRESHOLDS_PATH,
    load_thresholds,
    merged,
    missing_entries,
    save_thresholds,
)

EVAL = ROOT / "data" / "eval"
CALIB = EVAL / "probe_calib"
RUNS = CALIB / "runs"
MLX = ROOT / "data" / "mlx"

#: 動作点の条件 (現行の根拠は briefing.py / pir_daily_focus.py のコメント、
#: assign は F1 の 26B 単体 0.5 の適合率)
THIN_MAX_FALSE_FIRE = 0.03
CLAIM_MAX_FALSE_FIRE = 0.05
ASSIGN_PRECISION_FLOOR = 0.882
ORG_MAX_FALSE_SAME = 0.0
#: 合格線 (s24_plan §4.2)
AUC_TOLERANCE = 0.02
BOTH_CHOICES_MIN_RATE = 0.95
ORG_RECALL_FLOOR = 0.85

TASKS = ("thin", "claim", "assign", "org")
TASK_STEP = {
    "thin": "thin_body_judge",
    "claim": "pir_focus_claim_judge",
    "assign": "assign_evidence_judge",
    "org": "org_identity_judge",
}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_name(model: str) -> str:
    return model.replace(":", "_").replace("/", "_")


# ---------------------------------------------------------------------------
# 較正集 (既存の Opus 正解集を流用 + 足した分)
# ---------------------------------------------------------------------------


def load_thin() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for split, items_f, audit_f in (
        ("p2", "p2_items.json", "p2_audit.jsonl"),
        ("p2b", "p2b_items.json", "p2b_audit.jsonl"),
    ):
        items = {i["key"]: i for i in json.loads((EVAL / items_f).read_text(encoding="utf-8"))}
        for a in read_jsonl(EVAL / audit_f):
            it = items.get(a["key"])
            if a["body_type"] == "boundary" or it is None:
                continue
            rows.append(
                {
                    "id": f"{split}:{a['key']}",
                    "group": a["key"],
                    "label": 1 if a["body_type"] == "content" else 0,
                    "weight": 1.0,
                    "title": it["title"],
                    "body": it["body"],
                    "article_id": a["key"],
                    "body_type": a["body_type"],
                    "source": split,
                }
            )
    return rows + read_jsonl(CALIB / "extra" / "thin_body_extra.jsonl")


def load_claim() -> list[dict[str, Any]]:
    gold = {g["claim_id"]: g for g in read_jsonl(EVAL / "s5_claims_gold.jsonl")}
    rows: list[dict[str, Any]] = []
    for c in json.loads((EVAL / "s5_claims.json").read_text(encoding="utf-8")):
        g = gold.get(c["claim_id"])
        if c["variant"] != "new" or g is None or g["boundary"]:
            continue
        rows.append(
            {
                "id": c["claim_id"],
                "group": c["key"],
                "label": 1 if g["supported"] else 0,
                "weight": 1.0,
                "sentence": c["sentence"],
                "matches": c["matches"],
                "source": "s5_new",
            }
        )
    return rows + read_jsonl(CALIB / "extra" / "pir_focus_claim_extra.jsonl")


def load_assign() -> list[dict[str, Any]]:
    return [
        {
            "id": r["key"],
            "group": r["article_id"],
            "label": 1 if r["label"] == "same" else 0,
            "weight": 1.0,
            "situation_title": r["situation_title"],
            "anchors": r["situation_anchors"],
            "article_title": r["article_title"],
            "article_summary": r.get("article_summary", ""),
            "source": "assign_probe",
        }
        for r in read_jsonl(MLX / "assign_probe_input.jsonl")
    ]


def load_org() -> list[dict[str, Any]]:
    return [
        {
            "id": f"org:{r['id']}",
            "group": f"org:{r['id']}",
            "label": 1 if r["y_strict"] else 0,
            "weight": 1.0,
            "a": r["shown_name_a"],
            "ca": r["ctx_a"],
            "b": r["shown_name_b"],
            "cb": r["ctx_b"],
            "lab": r["label"],
            "source": "org_identity_labels_opus",
        }
        for r in read_jsonl(EVAL / "org_identity_labels_opus.jsonl")
    ]


LOADERS: dict[str, Callable[[], list[dict[str, Any]]]] = {
    "thin": load_thin,
    "claim": load_claim,
    "assign": load_assign,
    "org": load_org,
}


def calls_in(task: str, row: dict[str, Any]) -> int:
    if task == "claim":
        return len(row["matches"])
    return 2 if task == "org" else 1


def cmd_count() -> int:
    total = 0
    for task in TASKS:
        rows = LOADERS[task]()
        pos = sum(r["label"] == (0 if task in ("thin", "claim") else 1) for r in rows)
        calls = sum(calls_in(task, r) for r in rows)
        total += calls
        label = "No 側 (薄い/裏付けなし)" if task in ("thin", "claim") else "Yes 側 (同一/証拠)"
        print(f"{task}: 項目 {len(rows)} / 目的側の正例 {pos} ({label}) / 判定呼び出し {calls}")
    print(f"1 モデルあたり {total} 回")
    return 0


def cmd_build() -> int:
    CALIB.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {"created": datetime.now(UTC).strftime("%Y-%m-%d"), "files": {}}
    for task in TASKS:
        rows = LOADERS[task]()
        path = CALIB / f"{TASK_STEP[task]}.jsonl"
        path.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
        )
        manifest["files"][path.name] = {
            "rows": len(rows),
            "sha256": file_sha(path),
            "label_counts": {str(k): sum(r["label"] == k for r in rows) for k in (0, 1)},
        }
    (CALIB / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
# run (GPU): 本番の関数を、モデルだけ差し替えて呼ぶ
# ---------------------------------------------------------------------------


class BothChoicesRecorder:
    """probe_choice の読み出しで、Yes/No の両方が top_logprobs に入ったかを数える (逐次実行)。"""

    def __init__(self) -> None:
        self.flags: list[bool] = []

    def wrap(self, original: Callable[..., Any]) -> Callable[..., Any]:
        from src.tools.decision_probe import _choice_for_token

        def inner(candidates: list[tuple[str, float]], choices: tuple[str, ...]) -> Any:
            seen = {_choice_for_token(t, choices) for t, _ in candidates} - {None}
            self.flags.append(len(seen) == len(choices))
            return original(candidates, choices)

        return inner

    def drain(self) -> float | None:
        flags, self.flags = self.flags, []
        return sum(flags) / len(flags) if flags else None


def prepare_model(model: str) -> None:
    """denylist を通し、一時タグ (kuebiko-sft:*) は raw + JSON 前置きの経路で測る。

    一時タグは環境変数 SFT_TASK_PREFIX_MODELS に足す。
    """
    from src.tools.llm_client import validate_model_name
    from src.tools.task_prefix import PREFIX_TRAINED_MODELS

    validate_model_name(model)
    if model.startswith("kuebiko-sft:") and model not in PREFIX_TRAINED_MODELS:
        extra = {m for m in os.environ.get("SFT_TASK_PREFIX_MODELS", "").split(",") if m} | {model}
        os.environ["SFT_TASK_PREFIX_MODELS"] = ",".join(sorted(extra))


async def probe_row(task: str, r: dict[str, Any], config: Any) -> dict[str, Any] | None:
    import src.tools.decision_probe as dp
    from src.assessment.assign_probe import probe_assign_p_yes
    from src.digest.pir_daily_focus import _claim_article_block, _claim_question
    from src.eventnews.org_identity import _probe_logit, same_event_question, same_org_question
    from src.pipeline.briefing import _thin_body_probe_question
    from src.pir.evaluator import PirMatch
    from src.tools.model_tiers import Step

    if task == "thin":
        pr = await dp.probe_choice(
            Step.THIN_BODY_JUDGE,
            _thin_body_probe_question(r["title"], r["body"].strip()),
            ("Yes", "No"),
            config=config,
        )
        return None if pr is None else {"p": pr["Yes"]}
    if task == "claim":
        scores = []
        for i, m in enumerate(r["matches"], start=1):
            pm = PirMatch(
                article_id=m["article_id"],
                title=m["title"],
                url=m["url"],
                feed_title=m["feed_title"],
                importance=m["importance"],
                posted_channel=m.get("posted_channel"),
                created_at=m["created_at"],
                matched_via=tuple(m["matched_via"]),
                feed_url=m.get("feed_url", ""),
                summary=m.get("summary", ""),
            )
            pr = await dp.probe_choice(
                Step.PIR_FOCUS_CLAIM_JUDGE,
                _claim_question(r["sentence"], _claim_article_block(i, pm)),
                ("Yes", "No"),
                config=config,
            )
            if pr is not None:
                scores.append(pr["Yes"])
        return {"p": max(scores)} if scores else None
    if task == "assign":
        p = await probe_assign_p_yes(
            situation_title=r["situation_title"],
            anchors=frozenset(r["anchors"]),
            article_title=r["article_title"],
            article_summary=r["article_summary"],
            config=config,
        )
        return None if p is None else {"p": p}
    ev = await _probe_logit(
        same_event_question(r["a"], r["ca"], r["b"], r["cb"]),
        config=config,
        db_path=None,
        client=None,
    )
    og = await _probe_logit(
        same_org_question(r["a"], r["ca"], r["b"], r["cb"]),
        config=config,
        db_path=None,
        client=None,
    )
    return None if ev is None or og is None else {"ev": ev, "og": og}


async def show_raw_once(model: str, config: Any) -> None:
    """経路一致の確認 (s24_plan §4.2-1): 生の top_logprobs を 1 件見る。"""
    import ollama

    import src.tools.decision_probe as dp

    question = (
        "以下はニュース記事のタイトルと本文です。\n\nタイトル: 例\n\n本文:\n例の本文です。\n\n"
        "質問: この本文は中身を具体的に書いているか。"
    )
    client = ollama.AsyncClient(host=config.ollama_base_url, timeout=120.0)
    try:
        if dp._is_json_biased(model):  # noqa: SLF001
            res: Any = await client.generate(
                model=model,
                prompt=dp._json_biased_prompt(question, ("Yes", "No")),
                raw=True,
                logprobs=True,  # noqa: SLF001
                top_logprobs=5,
                options={"num_predict": 1, "temperature": 0},
            )
            path = "raw + JSON 前置き"
        else:
            res = await client.chat(
                model=model,
                messages=dp._default_messages(question, ("Yes", "No")),
                think=False,
                logprobs=True,  # noqa: SLF001
                top_logprobs=5,
                options={"num_predict": 1, "temperature": 0},
            )
            path = "chat"
    finally:
        await client.close()  # type: ignore[no-untyped-call]
    top = (
        [(t.token, round(t.logprob, 3)) for t in (res.logprobs[0].top_logprobs or ())]
        if res.logprobs
        else []
    )
    print(f"[経路確認] model={model} path={path} top_logprobs={top}", flush=True)


async def cmd_run(model: str) -> int:
    import src.tools.decision_probe as dp
    from src.config_loader import AppConfig

    prepare_model(model)
    config = AppConfig()
    out = RUNS / f"{safe_name(model)}.jsonl"
    RUNS.mkdir(parents=True, exist_ok=True)
    done = {(r["task"], r["id"]) for r in read_jsonl(out)}
    recorder = BothChoicesRecorder()
    dp.resolve_step_model = lambda step, db_path=None: model  # type: ignore[assignment]
    dp._extract_choice_probabilities = recorder.wrap(dp._extract_choice_probabilities)  # type: ignore[assignment]  # noqa: SLF001
    await show_raw_once(model, config)
    failed = 0
    with out.open("a", encoding="utf-8") as fh:
        for task in TASKS:
            rows = [r for r in LOADERS[task]() if (task, r["id"]) not in done]
            print(f"[{model}] {task}: 対象 {len(rows)}", flush=True)
            for r in rows:
                recorder.drain()
                got = await probe_row(task, r, config)
                if got is None:
                    failed += 1
                    continue
                fh.write(
                    json.dumps(
                        {
                            "model": model,
                            "task": task,
                            "id": r["id"],
                            **got,
                            "both_rate": recorder.drain(),
                        }
                    )
                    + "\n"
                )
                fh.flush()
    print(f"失敗 {failed} (失敗行は書かない。再実行で再試行)", flush=True)
    return 0


# ---------------------------------------------------------------------------
# fit / gate (CPU)
# ---------------------------------------------------------------------------


def joined(task: str, model: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    runs = {r["id"]: r for r in read_jsonl(RUNS / f"{safe_name(model)}.jsonl") if r["task"] == task}
    return [(r, runs[r["id"]]) for r in LOADERS[task]() if r["id"] in runs]


def op_dict(op: Operating | None) -> dict[str, float | None] | None:
    return (
        None
        if op is None
        else {
            "threshold": op.threshold,
            "recall": op.recall,
            "false_fire": op.false_fire,
            "precision": op.precision,
        }
    )


def score_stats(rows: list[Row]) -> dict[str, Any]:
    band, n_band = confident_band_accuracy(rows)
    a, b = platt_fit(rows)
    return {
        "auc": weighted_auc(rows),
        "ece": ece(rows),
        "ece_platt": ece(platt_apply(rows, a, b)),
        "platt": [a, b],
        "confident_band_accuracy": band,
        "confident_band_n": n_band,
    }


def fit_single(task: str, pairs: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
    rows = [
        Row(str(r["group"]), int(r["label"]), float(run["p"]), float(r.get("weight", 1.0)))
        for r, run in pairs
    ]
    low = task in ("thin", "claim")
    cap = THIN_MAX_FALSE_FIRE if task == "thin" else CLAIM_MAX_FALSE_FIRE
    const = CURRENT_PRODUCTION_CONSTANTS[_step(task)][MAIN_QUESTION]
    if low:
        threshold = pick_low_flag_threshold(rows, cap)
        oof, folds = out_of_fold_low_flag(rows, cap)
        at_current = operating_low_flag(rows, const)
        at_fit = operating_low_flag(rows, threshold) if threshold is not None else None
    else:
        threshold = pick_high_accept_threshold(rows, ASSIGN_PRECISION_FLOOR)
        oof, folds = out_of_fold_high_accept(rows, ASSIGN_PRECISION_FLOOR)
        at_current = operating_high_accept(rows, const)
        at_fit = operating_high_accept(rows, threshold) if threshold is not None else None
    both = [run["both_rate"] for _, run in pairs if run.get("both_rate") is not None]
    return {
        "n": len(rows),
        "target_label": 0 if low else 1,
        "target_n": sum(r.label == (0 if low else 1) for r in rows),
        **score_stats(rows),
        "threshold": {MAIN_QUESTION: threshold},
        "at_fit_threshold": op_dict(at_fit),
        "at_current_constant": op_dict(at_current),
        "out_of_fold": op_dict(oof),
        "fold_thresholds": folds,
        "both_choices_rate": sum(both) / len(both) if both else None,
    }


def fit_org(pairs: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
    rows = [
        OrgRow(
            str(r["group"]),
            int(r["label"]),
            float(run["ev"]),
            float(run["og"]),
            float(r.get("weight", 1.0)),
        )
        for r, run in pairs
    ]
    picked = pick_org_thresholds(rows, ORG_MAX_FALSE_SAME)
    oof, folds = out_of_fold_org(rows, ORG_MAX_FALSE_SAME)
    cur = CURRENT_PRODUCTION_CONSTANTS[_step("org")]

    def at(ev: float, og: float) -> dict[str, float | None]:
        pos = [r for r in rows if r.label == 1]
        neg = [r for r in rows if r.label == 0]
        caught = sum(org_same(r, ev, og) for r in pos)
        wrong = sum(org_same(r, ev, og) for r in neg)
        return {
            "recall": caught / len(pos) if pos else None,
            "false_same": wrong / len(neg) if neg else None,
        }

    # AUC は両 logit の小さい方 (本番の min(ev-12, og+4) と同じ形) で見る
    margin = [
        Row(r.group, r.label, min(r.event - cur["event_logit"], r.org - cur["org_logit"]), r.weight)
        for r in rows
    ]
    both = [run["both_rate"] for _, run in pairs if run.get("both_rate") is not None]
    return {
        "n": len(rows),
        "target_label": 1,
        "target_n": sum(r.label == 1 for r in rows),
        "auc_margin": weighted_auc(margin),
        "threshold": None if picked is None else {"event_logit": picked[0], "org_logit": picked[1]},
        "at_fit_threshold": None if picked is None else at(*picked),
        "at_current_constant": at(cur["event_logit"], cur["org_logit"]),
        "out_of_fold": op_dict(oof),
        "fold_thresholds": folds,
        "both_choices_rate": sum(both) / len(both) if both else None,
    }


def _step(task: str) -> Any:
    from src.tools.model_tiers import Step

    return Step(TASK_STEP[task])


def cmd_fit(model: str, write: bool) -> int:
    report: dict[str, Any] = {
        "model": model,
        "created": datetime.now(UTC).strftime("%Y-%m-%d"),
        "calib_sha256": {},
        "steps": {},
    }
    thresholds = load_thresholds()
    for task in TASKS:
        pairs = joined(task, model)
        path = CALIB / f"{TASK_STEP[task]}.jsonl"
        report["calib_sha256"][path.name] = file_sha(path) if path.exists() else None
        if not pairs:
            report["steps"][TASK_STEP[task]] = {"n": 0, "note": "runs が無い"}
            continue
        result = fit_org(pairs) if task == "org" else fit_single(task, pairs)
        report["steps"][TASK_STEP[task]] = result
        values = result.get("threshold") or {}
        if write and values and all(v is not None for v in values.values()):
            thresholds = merged(
                thresholds, model, _step(task), {k: float(v) for k, v in values.items()}
            )
    CALIB.mkdir(parents=True, exist_ok=True)
    out = CALIB / f"{safe_name(model)}.report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if write:
        save_thresholds(thresholds)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(
        f"→ {out}"
        + (
            f" / {THRESHOLDS_PATH} に {model} の項を書いた"
            if write
            else " (しきい値は書いていない: --write)"
        )
    )
    return 0


def gate_step(cand: dict[str, Any], base: dict[str, Any], step: str) -> dict[str, bool | None]:
    """合格線 ①〜④。None = 測れていない (不合格扱いにしない代わりに報告に出す)。"""
    if step == "org_identity_judge":
        auc_ok = _ge(cand.get("auc_margin"), base.get("auc_margin"), -AUC_TOLERANCE)
        oof = cand.get("out_of_fold") or {}
        precision_ok = oof.get("false_fire") == 0.0 if oof else None
        recall_ok = (
            (oof.get("recall") is not None and oof["recall"] >= ORG_RECALL_FLOOR) if oof else None
        )
        band_ok = None
    else:
        auc_ok = _ge(cand.get("auc"), base.get("auc"), -AUC_TOLERANCE)
        band_ok = _ge(cand.get("confident_band_accuracy"), base.get("confident_band_accuracy"), 0.0)
        oof, cur = cand.get("out_of_fold") or {}, base.get("at_current_constant") or {}
        recall_ok = _ge(oof.get("recall"), cur.get("recall"), 0.0)
        precision_ok = _ge(oof.get("precision"), cur.get("precision"), 0.0)
    both = cand.get("both_choices_rate")
    return {
        "auc": auc_ok,
        "confident_band": band_ok,
        "recall": recall_ok,
        "precision": precision_ok,
        "both_choices": None if both is None else both >= BOTH_CHOICES_MIN_RATE,
    }


def _ge(a: float | None, b: float | None, delta: float) -> bool | None:
    return None if a is None or b is None else a >= b + delta


def cmd_gate(model: str, baseline: str) -> int:
    cand = json.loads((CALIB / f"{safe_name(model)}.report.json").read_text(encoding="utf-8"))[
        "steps"
    ]
    base = json.loads((CALIB / f"{safe_name(baseline)}.report.json").read_text(encoding="utf-8"))[
        "steps"
    ]
    verdicts: dict[str, Any] = {}
    for step in TASK_STEP.values():
        if "threshold" not in cand.get(step, {}) or "threshold" not in base.get(step, {}):
            verdicts[step] = {"pass": False, "reason": "report が揃っていない"}
            continue
        checks = gate_step(cand[step], base[step], step)
        verdicts[step] = {
            "checks": checks,
            "pass": all(v is True for k, v in checks.items() if v is not None)
            and any(v is True for v in checks.values()),
        }
    print(
        json.dumps(
            {"model": model, "baseline": baseline, "verdicts": verdicts},
            ensure_ascii=False,
            indent=2,
        )
    )
    print("1 本でも満たさない判定は、その判定だけ旧モデルのまま。")
    return 0 if all(v["pass"] for v in verdicts.values()) else 1


def cmd_check() -> int:
    from src.tools.model_tiers import resolve_step_model

    table = load_thresholds()
    bad = 0
    for step in PROBE_STEP_QUESTIONS:
        model = resolve_step_model(step)
        lacking = [m for m in missing_entries(model, table) if m.startswith(step.value)]
        print(f"{step.value}: 解決モデル {model} / 不足 {lacking or 'なし'}")
        bad += bool(lacking)
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("command", choices=["count", "build", "run", "fit", "gate", "check"])
    ap.add_argument("--model")
    ap.add_argument("--baseline")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    if a.command in ("run", "fit", "gate") and not a.model:
        ap.error("--model が要る")
    if a.command == "gate" and not a.baseline:
        ap.error("--baseline が要る")
    if a.command == "count":
        return cmd_count()
    if a.command == "build":
        return cmd_build()
    if a.command == "run":
        return asyncio.run(cmd_run(a.model))
    if a.command == "fit":
        return cmd_fit(a.model, a.write)
    if a.command == "gate":
        return cmd_gate(a.model, a.baseline)
    return cmd_check()


if __name__ == "__main__":
    raise SystemExit(main())
