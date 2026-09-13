#!/usr/bin/env python3
"""事象ニュース凍結 39 件の対読審判 (2026-09-13)。

2 腕の structured 出力を、外部 LLM (claude-code-bridge 経由の Sonnet、利用者承認済み) に
**同一入力の候補記事**を見せて比較させる。件数指標は品質そのものではない (レシピ是正した
生徒は件数で勝っても偽の対比・言い換え重複・全空を出す) ので、差し替え判定は対読で行う。

- 提示順を入れ替えて 2 回採点し、順序バイアスを消す (両回で同じ勝者 = 確定、割れたら引き分け)
- 観点: 接地 (候補記事に無い主張・同じ値を相違とする偽の対比の数) / 留保・未解明点の網羅 /
  冗長 (同じ主張の繰り返し) / 総合
- 出力: data/mlx/judge_<A>_vs_<B>.json (item ごとの両回の判定と集計)
使い方: uv run python scripts/judge_eventnews_pairwise.py --a data/mlx/eval_ollama_sft.json:N1 \
          --b data/mlx/eval_ollama_n17s1.json:s1 [--limit N] [--model claude-sonnet-5]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.tools.claude_code_client import ClaudeCodeClient  # noqa: E402

_SYSTEM = (
    "あなたは CTI 記事の要約品質を審査する編集者です。与えられた候補記事 (入力) に対する "
    "2 つの構造化要約 A と B を比較します。判断は入力に書かれている内容のみを根拠にし、"
    "自分の知識で補わないでください。"
)

_TEMPLATE = """# 入力 (候補記事を含むプロンプト)
{prompt}

# 要約 A
{a}

# 要約 B
{b}

# 審査
各要約について次を数え、最後に総合の勝者を決めてください。
1. 接地違反: 入力に根拠が無い主張、または「両記事とも同じ値なのに相違点として挙げる」偽の対比の件数
2. 網羅: 入力から読み取れる重要な留保 (原文の但し書き・未確認の表現) と未解明点を、
   どれだけ拾えているか (0-5)
3. 冗長: 同じ主張の言い換え繰り返しの件数
4. 総合: 読者 (脅威情報の分析者) にとってどちらが有用か。件数の多さではなく、
   正確さと重要事項の網羅で判断
"""


class ArmScore(BaseModel):
    grounding_violations: int = Field(ge=0)
    coverage: int = Field(ge=0, le=5)
    redundancy: int = Field(ge=0)


class Verdict(BaseModel):
    a: ArmScore
    b: ArmScore
    winner: Literal["A", "B", "tie"]
    reason: str


def _load(spec: str) -> tuple[str, list[dict[str, str]]]:
    path, _, label = spec.partition(":")
    rows = json.loads(Path(path).read_text())
    return label or Path(path).stem, rows


async def _judge_one(client: ClaudeCodeClient, prompt: str, a: str, b: str) -> Verdict:
    return await client.generate_structured(
        prompt=_TEMPLATE.format(prompt=prompt, a=a, b=b),
        schema=Verdict,
        system=_SYSTEM,
        temperature=0.0,
        max_tokens=1200,
    )


async def main_async(args: argparse.Namespace) -> int:
    label_a, rows_a = _load(args.a)
    label_b, rows_b = _load(args.b)
    n = min(len(rows_a), len(rows_b), args.limit or 10**9)
    client = ClaudeCodeClient(model=args.model, bridge_url=args.bridge_url, timeout_seconds=180)
    out_path = Path(f"data/mlx/judge_{label_a}_vs_{label_b}.json")
    results: list[dict[str, Any]] = []
    if out_path.exists() and not args.fresh:
        results = json.loads(out_path.read_text())
    done = {r["index"] for r in results}
    for i in range(n):
        if i in done:
            continue
        prompt = rows_a[i]["prompt"]
        out_a = rows_a[i].get("ollama_generated") or ""
        out_b = rows_b[i].get("ollama_generated") or ""
        v1 = await _judge_one(client, prompt, out_a, out_b)  # A=label_a
        v2 = await _judge_one(client, prompt, out_b, out_a)  # A=label_b (入替)
        w1 = {"A": label_a, "B": label_b, "tie": "tie"}[v1.winner]
        w2 = {"A": label_b, "B": label_a, "tie": "tie"}[v2.winner]
        final = w1 if w1 == w2 else "tie"
        results.append(
            {
                "index": i,
                "pass1": v1.model_dump(),
                "pass2_swapped": v2.model_dump(),
                "winner": final,
                # 腕ごとの平均用 (入替後の a/b を元のラベルに戻す)
                label_a: {"p1": v1.a.model_dump(), "p2": v2.b.model_dump()},
                label_b: {"p1": v1.b.model_dump(), "p2": v2.a.model_dump()},
            }
        )
        out_path.write_text(json.dumps(results, ensure_ascii=False, indent=1))
        print(
            f"{i:2d} {final:4s} | {label_a}: gv {v1.a.grounding_violations}/"
            f"{v2.b.grounding_violations} cov {v1.a.coverage}/{v2.b.coverage} | "
            f"{label_b}: gv {v1.b.grounding_violations}/{v2.a.grounding_violations} "
            f"cov {v1.b.coverage}/{v2.a.coverage}",
            flush=True,
        )
    c = Counter(r["winner"] for r in results)
    print(
        f"\n集計 ({len(results)} 件): {label_a} {c[label_a]} / {label_b} {c[label_b]} / "
        f"tie {c['tie']}"
    )
    for lab in (label_a, label_b):
        gv = [r[lab][p]["grounding_violations"] for r in results for p in ("p1", "p2")]
        cov = [r[lab][p]["coverage"] for r in results for p in ("p1", "p2")]
        red = [r[lab][p]["redundancy"] for r in results for p in ("p1", "p2")]
        print(
            f"  {lab:6s} 接地違反 {sum(gv) / len(gv):.2f}  網羅 {sum(cov) / len(cov):.2f}  "
            f"冗長 {sum(red) / len(red):.2f}"
        )
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--a", required=True, help="eval json:ラベル (基準腕)")
    p.add_argument("--b", required=True, help="eval json:ラベル (挑戦腕)")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--model", default="sonnet", help="bridge のモデル名 (sonnet / opus / haiku)")
    p.add_argument("--bridge-url", default="http://127.0.0.1:8010")
    p.add_argument("--fresh", action="store_true", help="既存の結果を捨てて最初から")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
