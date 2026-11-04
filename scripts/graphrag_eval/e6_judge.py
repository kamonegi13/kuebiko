"""E6: 整合の点検の候補を Opus が盲検で判定し、規則ごとの誤りの割合を集計する (prereg graph_consistency_e6.md)。

    uv run python -m scripts.graphrag_eval.e6_judge --dir data/eval/graph_e6_2026_11 [--check-only | --dry-run]

標本は e6_sample.py が書いた sample.jsonl。gold.jsonl の sha256 は標本の抽出の後・Opus の前に
manifest の `gold_sha256_pre` ではなく `sample_sha256` として残る (sample 側)。判定後の gold の sha256 は
判定が終わってから manifest に書く。同じ事象・組が複数の層に入る標本は 1 回だけ判定し、属する層すべてに数える。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from scripts.graphrag_eval.common import (
    ROOT,
    BudgetExceededError,
    OpusBudget,
    make_judge,
    now_iso,
    require_prereg,
    write_json,
)
from scripts.replay._stats import wilson_interval

PREREG = "graph_consistency_e6"
MAX_TOKENS = 1500
ADOPT_AT = 0.5
DROP_AT = 0.125
MIN_VALID_N = 5
MIN_ADOPTED_RULES = 2
MIN_OVERALL = 0.30
ERROR = "本当の誤り"
OK = "正常"
UNSURE = "判断できない"
CONTROL = "control"

RULE_DEFS: Mapping[str, str] = {
    "C1": "1 つの事象 (1 つの事案の報道の束) の中で、信頼できる主題 (アクター) のタグが 2 種以上ある。主題の違うものが 1 つの事象に混ざっていないか。",
    "C2": "1 つの事象の構成記事の種別 (インシデント報道・勧告・研究など) が 2 種以上ある。本来別の事象の記事が束ねられていないか。",
    "C3": "1 つの事象の被害国が 2 種以上ある。国の違う別々の被害が 1 つの事象に混ざっていないか。",
    "C4": "記事の種別が勧告・統計・まとめなのに、特定のアクターが主題として付いている。そのアクターの付与は誤りか。",
    "C5": "1 つの事象の焦点 (中心となる固有名・行為) から外れた記事が 2 割以上ある。外れた記事は本当にこの事象の話ではないか。",
    "C6": "同じ統合先に吸収された 2 つの事象の焦点が食い違っている。この 2 つは本来別の事象で、統合は誤りか。",
    "C10": "線で結ばれた 2 つの事象の片側が、複数の独立した事案の束 (まとめ記事) である。この線は誤りか。",
    CONTROL: "同じアクターを主題に共有するが、記事の種別が重ならない 2 つの事象。この組には食い違いがあり、点検が拾うべき誤りか。",
}

E6_RUBRIC = """あなたは CTI 部門の上級分析官です。サイバー脅威報道の整理結果に対する自動点検が、次の規則に当てはまる候補を挙げました。
その候補が「本当の誤り」(整理の結果が実際に間違っていて、直すべき) か「正常」(規則には当たるが整理は妥当) かを、示された記事の中身から判断します。

判定は 3 値です。
- 本当の誤り: 規則が疑う箇所が実際に誤っている (別の事案が混ざっている・付与が誤っている・統合が誤っている等)
- 正常: 規則には当たるが、整理は妥当 (同じ事案の別の側面・正しい付与・妥当な統合等)
- 判断できない: 示された材料だけでは決められない

規則の文言だけでなく、記事の本文要約に基づいて判断してください。点検の根拠として示された値は機械が出したもので、正しいとは限りません。
規則に当たること自体を誤りの証拠にしないでください。理由は 1-2 文で書きます。"""

E6_QUESTION = "規則 {layer}: {definition}\n候補を見て、これは本当の誤りか判定してください。"


class E6Verdict(BaseModel):
    model_config = ConfigDict(extra="ignore")
    verdict: Literal["本当の誤り", "正常", "判断できない"]
    reason: str = ""


def unit_id(row: Mapping[str, Any]) -> str:
    return f"{row['unit']}:{row['key']}"


def blind_inputs(row: Mapping[str, Any]) -> dict[str, Any]:
    """Opus に渡す入力。事象 id・層の名前は含めない (規則の定義文は質問側)。"""
    mats = [
        {k: v for k, v in m.items() if k not in ("event_id", "article_id")}
        for m in row["materials"]
    ]
    return {"候補の根拠 (機械の出力)": row["evidence"], "材料": mats}


def question_for(row: Mapping[str, Any]) -> str:
    layer = str(row["layer"])
    return E6_QUESTION.format(
        layer="(対照)" if layer == CONTROL else layer, definition=RULE_DEFS[layer]
    )


def dedupe(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """同じ単位は最初の層の行で 1 回だけ判定する。"""
    seen: dict[str, Mapping[str, Any]] = {}
    for r in rows:
        seen.setdefault(unit_id(r), r)
    return list(seen.values())


async def run(rows: Sequence[Mapping[str, Any]], budget: OpusBudget, out: Path) -> list[str]:
    judge = make_judge(E6Verdict, E6_RUBRIC, budget, max_tokens=MAX_TOKENS)
    failed: list[str] = []
    gold: dict[str, dict[str, Any]] = {}

    async def one(row: Mapping[str, Any]) -> None:
        v = await judge(question_for(row), blind_inputs(row))
        uid = unit_id(row)
        if not isinstance(v, E6Verdict):
            failed.append(uid)
            return
        gold[uid] = {"unit_id": uid, "verdict": v.verdict, "reason": v.reason}

    try:
        await asyncio.gather(*[one(r) for r in rows])
    finally:
        with out.open("w", encoding="utf-8") as fh:
            for k in sorted(gold):
                fh.write(json.dumps(gold[k], ensure_ascii=False) + "\n")
    return failed


def rule_decision(errors: int, valid_n: int) -> str:
    if valid_n < MIN_VALID_N:
        return "保留(有効n不足)"
    rate = errors / valid_n
    if rate >= ADOPT_AT:
        return "採用"
    if rate <= DROP_AT:
        return "捨てる"
    return "保留"


def summarize(
    sample_rows: Sequence[Mapping[str, Any]],
    gold: Mapping[str, Mapping[str, Any]],
    population: Mapping[str, int],
) -> dict[str, Any]:
    layers: dict[str, Any] = {}
    for layer in dict.fromkeys(str(r["layer"]) for r in sample_rows):
        verdicts = [
            gold[unit_id(r)]["verdict"]
            for r in sample_rows
            if r["layer"] == layer and unit_id(r) in gold
        ]
        errors = verdicts.count(ERROR)
        unsure = verdicts.count(UNSURE)
        valid = len(verdicts) - unsure
        iv = wilson_interval(errors, valid)
        layers[layer] = {
            "judged": len(verdicts),
            "errors": errors,
            "normal": verdicts.count(OK),
            "unsure": unsure,
            "valid_n": valid,
            "error_rate": iv.point if valid else None,
            "wilson95": [iv.low, iv.high] if valid else None,
            "N_k": population.get(layer),
            "decision": "基準線" if layer == CONTROL else rule_decision(errors, valid),
        }
    adopted = [k for k, v in layers.items() if v["decision"] == "採用"]
    weight = sum(layers[k]["N_k"] or 0 for k in adopted)
    overall = (
        sum((layers[k]["N_k"] or 0) * layers[k]["error_rate"] for k in adopted) / weight
        if weight
        else None
    )
    ship = len(adopted) >= MIN_ADOPTED_RULES and overall is not None and overall >= MIN_OVERALL
    return {"layers": layers, "adopted": adopted, "overall": overall, "ship_inspection": ship}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, required=True)
    ap.add_argument("--check-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--estimate", type=int, default=55)
    args = ap.parse_args()
    d: Path = args.dir if args.dir.is_absolute() else ROOT / args.dir
    gate = require_prereg(PREREG, Path(__file__), d)
    print(json.dumps(gate, ensure_ascii=False))
    if args.check_only:
        return 0
    sample = d / "sample.jsonl"
    manifest_path = d / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    if manifest["sample_sha256"] != sha256(sample):
        raise RuntimeError("sample.jsonl が manifest の sample_sha256 と違う (抽出の後に変えた)")
    all_rows = load_rows(sample)
    rows = dedupe(all_rows)
    budget = OpusBudget("GRAPH-E6", args.estimate)
    print(
        f"標本 {len(all_rows)} 行 / 判定 {len(rows)} 件 / 見積もり {budget.estimate} / 上限 {budget.limit}"
    )
    if args.dry_run:
        return 0
    gold_path = d / "gold.jsonl"
    stopped = None
    try:
        failed = asyncio.run(run(rows, budget, gold_path))
    except BudgetExceededError as exc:
        stopped, failed = str(exc), []
    gold = {r["unit_id"]: r for r in load_rows(gold_path)}
    population = {k: v["N_after_exclusion"] for k, v in manifest["layers"].items()}
    summary = summarize(all_rows, gold, population)
    write_json(d / "summary.json", summary)
    write_json(
        d / "judge_manifest.json",
        {
            **gate,
            "item": "GRAPH-E6",
            "judge": "claude-opus-5-5",
            "sample_sha256": manifest["sample_sha256"],
            "gold_path": str(gold_path.relative_to(ROOT)),
            "gold_sha256": sha256(gold_path),
            "opus_calls_bridge": budget.used,
            "budget_limit": budget.limit,
            "failed": failed,
            "stopped": stopped,
            "finished_at": now_iso(),
        },
    )
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    print("Opus", budget.used, "失敗", len(failed), "停止", stopped)
    return 0 if stopped is None else 2


if __name__ == "__main__":
    sys.exit(main())
