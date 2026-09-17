#!/usr/bin/env python3
"""状況総括 凍結 15 窓の対読審判 (2026-09-17、n17c = CoT 混合の合否用)。

`scripts/eval_synthesis_judge_set.py` が出した 2 腕の生成物 (A = 対照、B = 候補) を、
外部 LLM (claude-code-bridge 経由の Sonnet) に **同じ estimate (render プロンプト)** を見せて
比較させる。CoT 腕の ``analysis_notes`` は読者に出さない欄なので**比較からは外し**、代わりに
決定論の関門 (5 観点の有無) で別に数える。

- 提示順を入れ替えて 2 回採点 (両回同じ勝者 = 確定、割れたら tie)
- 観点: 接地 (estimate に無い主張・順位の入替) / 確度語の逸脱 / 網羅 (変化した判定の取り漏れ) /
  道具側の操作の記述 (「claim を改訂した」等、禁止) / 冗長 / 総合
- 出力: data/mlx/judge_synth_<A>_vs_<B>.json
使い方: uv run python scripts/judge_synthesis_pairwise.py \\
          --a data/mlx/eval_synthesis_n17m30.json:M30 --b data/mlx/eval_synthesis_n17c.json:n17c
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.tools.claude_code_client import ClaudeCodeClient  # noqa: E402
from src.tools.llm_client import LLMError  # noqa: E402

SECTION_KEYS: tuple[str, ...] = (
    "headline",
    "weight_section",
    "chain_section",
    "cog_section",
    "spillover_section",
    "pir_section",
)
#: analysis_notes の 5 観点 (SYNTHESIS §41): 名称 → 検出パターン
NOTES_ASPECTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("出典", re.compile(r"出典|依存|媒体|ベンダ|裏取り|独立")),
    ("時制", re.compile(r"時制|本期間|過去|継続中|新規|以前")),
    ("前期指標", re.compile(r"指標|発火|前期|前回|継続性")),
    ("阻害判断", re.compile(r"阻害|判断|欠落|欠如|確度を")),
    ("日本露出", re.compile(r"日本|国内|自衛|防衛")),
)

_SYSTEM = (
    "あなたは CTI の状況総括 (narrative) の品質を審査する編集者です。与えられた estimate "
    "(確定済みの判定・確度・変化) に対する 2 つの総括 A と B を比較します。判断は estimate に"
    "書かれている内容のみを根拠にし、自分の知識で補わないでください。"
)

_TEMPLATE = """# 入力 (estimate を含む render プロンプト)
{prompt}

# 総括 A
{a}

# 総括 B
{b}

# 審査
各総括について次を数え、最後に総合の勝者を決めてください。
1. 接地違反: estimate に無い主張・帰属・断定、または掲載順位 (headline 指名) を勝手に変えた件数
2. 確度語の逸脱: 判定の confidence と本文の確度語 (可能性が高い / 可能性がある / 確証はなく) が
   食い違う件数 (要約だからと一段上げるのも逸脱)
3. 網羅: 【変化した判定】に挙がった件をどれだけ weight_section で扱えているか、
   継続・監視指標・SIR 対応を落としていないか (0-5)
4. 道具側の操作: 「claim を改訂した」「確度を引き上げた」「見立て転換が行われた」のように、
   世界側の事実でなく台帳の更新履歴を書いた件数
5. 冗長: 同じ主張の言い換え繰り返しの件数
6. 総合: 読者 (脅威情報の分析者) にとってどちらが有用か。長さではなく、正確さ・確度語の規律・
   重要事項の網羅で判断
"""


class ArmScore(BaseModel):
    grounding_violations: int = Field(ge=0)
    confidence_mismatch: int = Field(ge=0)
    coverage: int = Field(ge=0, le=5)
    tool_operation_mentions: int = Field(ge=0)
    redundancy: int = Field(ge=0)


class Verdict(BaseModel):
    a: ArmScore
    b: ArmScore
    winner: Literal["A", "B", "tie"]
    reason: str = ""


def visible_sections(output_json: str) -> str:
    """生成物 (JSON 文字列) から読者に出す欄だけを取り出して整形する (analysis_notes は外す)。"""
    try:
        data = json.loads(output_json)
    except json.JSONDecodeError:
        return output_json
    if not isinstance(data, dict):
        return output_json
    return "\n".join(f"[{k}]\n{str(data.get(k, '')).strip()}" for k in SECTION_KEYS)


def notes_gate(output_json: str) -> dict[str, bool]:
    """analysis_notes の 5 観点の有無 (決定論)。notes が無ければ全 False。"""
    try:
        data = json.loads(output_json)
    except json.JSONDecodeError:
        return {name: False for name, _ in NOTES_ASPECTS}
    notes = str(data.get("analysis_notes", "")) if isinstance(data, dict) else ""
    return {name: bool(pat.search(notes)) for name, pat in NOTES_ASPECTS}


def final_winner(w1: str, w2: str) -> str:
    """提示順入替の 2 回で一致したときだけ確定、割れたら tie。"""
    return w1 if w1 == w2 else "tie"


def _load(spec: str) -> tuple[str, dict[str, dict[str, str]]]:
    path, _, label = spec.partition(":")
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    return label or Path(path).stem, {r["key"]: r for r in rows}


async def _judge_one(client: ClaudeCodeClient, prompt: str, a: str, b: str) -> Verdict:
    last: Exception | None = None
    for _ in range(3):
        try:
            return await client.generate_structured(
                prompt=_TEMPLATE.format(prompt=prompt, a=a, b=b),
                schema=Verdict,
                system=_SYSTEM,
                temperature=0.0,
                max_tokens=1200,
            )
        except LLMError as exc:
            last = exc
    raise RuntimeError(f"審判が 3 回失敗: {last}")


async def main_async(args: argparse.Namespace) -> int:
    label_a, rows_a = _load(args.a)
    label_b, rows_b = _load(args.b)
    keys = sorted(set(rows_a) & set(rows_b))
    if args.limit:
        keys = keys[: args.limit]
    out_path = Path(f"data/mlx/judge_synth_{label_a}_vs_{label_b}.json")
    results: list[dict[str, Any]] = []
    if out_path.exists() and not args.fresh:
        results = json.loads(out_path.read_text(encoding="utf-8"))
    done = {r["key"] for r in results}
    client = ClaudeCodeClient(model=args.model, bridge_url=args.bridge_url, timeout_seconds=420)
    for key in keys:
        if key in done:
            continue
        prompt = rows_a[key]["prompt"]  # 対照腕のプロンプト = 非 CoT の render (同じ estimate)
        out_a = visible_sections(rows_a[key]["output"])
        out_b = visible_sections(rows_b[key]["output"])
        v1 = await _judge_one(client, prompt, out_a, out_b)
        v2 = await _judge_one(client, prompt, out_b, out_a)
        w1 = {"A": label_a, "B": label_b, "tie": "tie"}[v1.winner]
        w2 = {"A": label_b, "B": label_a, "tie": "tie"}[v2.winner]
        final = final_winner(w1, w2)
        gate_b = notes_gate(rows_b[key]["output"])
        results.append(
            {
                "key": key,
                "pass1": v1.model_dump(),
                "pass2_swapped": v2.model_dump(),
                "winner": final,
                f"notes_gate_{label_b}": gate_b,
                label_a: {"p1": v1.a.model_dump(), "p2": v2.b.model_dump()},
                label_b: {"p1": v1.b.model_dump(), "p2": v2.a.model_dump()},
            }
        )
        out_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

        def _fmt(x: ArmScore, y: ArmScore) -> str:
            return (
                f"gv {x.grounding_violations}/{y.grounding_violations} "
                f"conf {x.confidence_mismatch}/{y.confidence_mismatch} "
                f"cov {x.coverage}/{y.coverage}"
            )

        print(
            f"{key} {final:5s} | {label_a}: {_fmt(v1.a, v2.b)} | {label_b}: {_fmt(v1.b, v2.a)}"
            f" | notes {sum(gate_b.values())}/5",
            flush=True,
        )
    c = Counter(r["winner"] for r in results)
    print(
        f"\n集計 ({len(results)} 件): {label_a} {c[label_a]} / "
        f"{label_b} {c[label_b]} / tie {c['tie']}"
    )
    for lab in (label_a, label_b):
        vals = [r[lab][p] for r in results for p in ("p1", "p2") if lab in r]
        if not vals:
            continue
        mean = {k: sum(v[k] for v in vals) / len(vals) for k in vals[0]}
        print(
            f"  {lab}: 接地違反 {mean['grounding_violations']:.2f} / "
            f"確度逸脱 {mean['confidence_mismatch']:.2f} / 網羅 {mean['coverage']:.2f} / "
            f"操作記述 {mean['tool_operation_mentions']:.2f} / 冗長 {mean['redundancy']:.2f}"
        )
    gates: list[dict[str, bool]] = [
        g for r in results if isinstance(g := r.get(f"notes_gate_{label_b}"), dict)
    ]
    if gates:
        summary = ", ".join(
            f"{n} {sum(g[n] for g in gates)}/{len(gates)}" for n, _ in NOTES_ASPECTS
        )
        print(f"  {label_b} analysis_notes 5 観点 (観点あり件数): {summary}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--a", required=True, help="eval json:ラベル (対照腕 = 非 CoT)")
    p.add_argument("--b", required=True, help="eval json:ラベル (候補腕 = CoT)")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--model", default="sonnet")
    p.add_argument("--bridge-url", default="http://127.0.0.1:8010")
    p.add_argument("--fresh", action="store_true")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
