#!/usr/bin/env python3
"""事後合理化を防ぐ対読審判 (2026-09-20、SYNTHESIS §58)。

⚠ **現行の審判は疑わしい**。1 回の呼出で総合判定と観点別得点を同時に出す設計で、
文献 (arXiv:2605.23970 Faithful or Fabricated?) はこれを事後合理化を誘発する典型とする。
実際、当方の審判は接地違反で勝敗を **12 戦 12 勝で完全予測**していた。
**完璧に予測しすぎること自体が、結論が先にあった兆候**である。

本 script は 3 点を変える:

1. ⭐ **独立採点** — 各腕を**相手を見ずに単独で**採点する。比較の枠組みを外せば、
   「どちらを勝たせるか」に合わせて数字を作ることができない。
2. ⭐ **証拠の固定** — 違反はすべて要約本文からの逐語引用を伴わせ、**本文に実在しない
   引用は棄却**する (08-22 の引用実在関門と同じ思想。当時 59.9% が本文に不在だった)。
3. ⭐ **総合は別呼出** — 観点を確定させた後に判定する。

そのうえで「**独立採点した違反数が、なお勝敗を予測するか**」を見る。予測すれば接地という
説明は本物。予測しなくなれば、現行審判の説明は事後合理化だったことになる。

    docker exec kuebiko python /app/scripts/judge_eventnews_pbp.py \
        --a /app/data/mlx/eval_ollama_n17m30.json:M30 \
        --b /app/data/mlx/eval_ollama_n17c.json:n17c \
        --provider ollama --base-url http://192.168.1.100:11434 --model gemma3:12b
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import unicodedata
from math import comb
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from scripts.judge_eventnews_pairwise import PROVIDERS, build_judge_client  # noqa: E402
from src.tools.llm_client import LLMClient, LLMError  # noqa: E402


class Violation(BaseModel):
    """接地違反 1 件。**引用は要約本文からの逐語**でなければならない。"""

    quote: str = Field(description="要約本文から逐語で引用した、根拠のない記述")
    reason: str = Field(description="なぜ入力が支えていないか (1 文)")


class Audit(BaseModel):
    """1 つの要約への独立採点 (相手を見ない)。"""

    violations: list[Violation] = Field(default_factory=list)


class Verdict(BaseModel):
    winner: Literal["A", "B", "tie"]
    reason: str = ""


# ---------- 純粋関数 ----------


def _norm(s: str) -> str:
    """照合用の正規化 — 空白と全角半角の違いで正当な引用を落とさない。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s))


def verified_violations(violations: list[Any], summary: str) -> list[Any]:
    """引用が要約本文に実在する違反だけを残す (純粋関数)。

    ⭐ 審判にも引用実在の関門を掛ける。捏造された引用で違反数が水増しされると、
    その数字で腕を比べてしまう。
    """
    hay = _norm(summary)
    return [v for v in violations if v.quote and _norm(v.quote) in hay]


def mcnemar_p(a_wins: int, b_wins: int) -> float:
    """不一致ペアのみを使う両側二項検定 (McNemar)。

    ⭐ 引き分けは情報を持たないので検定に入れない (文献の指摘。当方は 39 窓中 21 が
    引き分けで、そこを含めると検出力の見積もりを誤る)。
    """
    n = a_wins + b_wins
    if n == 0:
        return 1.0
    k = max(a_wins, b_wins)
    return float(min(1.0, 2 * sum(comb(n, i) for i in range(k, n + 1)) / 2**n))


# ---------- プロンプト ----------

_AUDIT_SYSTEM = (
    "あなたは CTI 記事要約の事実確認者です。与えられた入力 (候補記事) だけを根拠にし、"
    "自分の知識で補わないでください。比較はしません。1 つの要約だけを点検します。"
)

_AUDIT_TEMPLATE = """# 入力 (候補記事)
{prompt}

# 点検する要約
{summary}

# 指示
上の要約のうち、**入力が支えていない記述**をすべて挙げてください。該当するのは:
- 入力に書かれていない事実を述べている
- 入力の記述から推論した含意を、事実として断定している
- 入力が留保している事柄を断定している
- 入力にない対比や因果を作っている (同じ値を「相違」として挙げるものを含む)

**各件について、要約本文から逐語で引用**してください。言い換えず、一字一句そのまま写します。
該当が無ければ空の list を返してください。**件数を揃えないでください**。"""

_VERDICT_SYSTEM = (
    "あなたは CTI 記事要約の編集者です。2 つの要約 A と B のどちらが読み物として"
    "優れているかを判断します。入力に書かれている内容のみを根拠にしてください。"
)

_VERDICT_TEMPLATE = """# 入力 (候補記事)
{prompt}

# 要約 A
{a}

# 要約 B
{b}

# 指示
どちらが優れているか 1 つ選び (A / B / tie)、理由を 2 文以内で述べてください。"""


async def _call(client: LLMClient, prompt: str, system: str, schema: type[BaseModel]) -> Any:
    last: LLMError | None = None
    for _ in range(3):
        try:
            return await client.generate_structured(
                prompt=prompt, schema=schema, system=system, temperature=0.0, max_tokens=1600
            )
        except LLMError as e:
            last = e
            await asyncio.sleep(5)
    assert last is not None
    raise last


def _load(spec: str) -> tuple[str, list[dict[str, Any]]]:
    path, _, label = spec.partition(":")
    return label or Path(path).stem, json.loads(Path(path).read_text(encoding="utf-8"))


async def main_async(args: argparse.Namespace) -> int:
    label_a, rows_a = _load(args.a)
    label_b, rows_b = _load(args.b)
    n = min(len(rows_a), len(rows_b), args.limit or 10**9)
    client = build_judge_client(provider=args.provider, model=args.model, base_url=args.base_url)
    out_path = Path(args.out)
    results: list[dict[str, Any]] = []
    if out_path.exists() and not args.fresh:
        results = json.loads(out_path.read_text(encoding="utf-8"))
    done = {r["index"] for r in results}

    for i in range(n):
        if i in done:
            continue
        prompt = rows_a[i]["prompt"]
        summaries = {
            label_a: rows_a[i].get("ollama_generated") or "",
            label_b: rows_b[i].get("ollama_generated") or "",
        }
        rec: dict[str, Any] = {"index": i}
        # --- 段 1: 独立採点 (相手を見ない) ---
        for label, summary in summaries.items():
            if not summary:
                continue
            audit = await _call(
                client,
                _AUDIT_TEMPLATE.format(prompt=prompt, summary=summary),
                _AUDIT_SYSTEM,
                Audit,
            )
            kept = verified_violations(list(audit.violations), summary)
            rec[label] = {
                "claimed": len(audit.violations),
                "verified": len(kept),
                "dropped_unquoted": len(audit.violations) - len(kept),
                "items": [{"quote": v.quote[:120], "reason": v.reason[:120]} for v in kept][:8],
            }
        # --- 段 2: 総合判定 (順序を入れ替えて 2 回) ---
        v1 = await _call(
            client,
            _VERDICT_TEMPLATE.format(prompt=prompt, a=summaries[label_a], b=summaries[label_b]),
            _VERDICT_SYSTEM,
            Verdict,
        )
        v2 = await _call(
            client,
            _VERDICT_TEMPLATE.format(prompt=prompt, a=summaries[label_b], b=summaries[label_a]),
            _VERDICT_SYSTEM,
            Verdict,
        )
        w1 = {"A": label_a, "B": label_b, "tie": "tie"}[v1.winner]
        w2 = {"A": label_b, "B": label_a, "tie": "tie"}[v2.winner]
        rec["winner"] = w1 if w1 == w2 else "tie"
        rec["pass1"] = v1.model_dump()
        rec["pass2_swapped"] = v2.model_dump()
        results.append(rec)
        out_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        va = rec.get(label_a, {}).get("verified", 0)
        vb = rec.get(label_b, {}).get("verified", 0)
        print(
            f"{i:3d} {rec['winner']:5s} | 独立採点の違反 {label_a} {va} / {label_b} {vb}",
            flush=True,
        )

    # --- 集計 ---
    wins = {k: sum(1 for r in results if r["winner"] == k) for k in (label_a, label_b, "tie")}
    print(
        f"\n集計 ({len(results)} 件): {label_a} {wins[label_a]} / "
        f"{label_b} {wins[label_b]} / tie {wins['tie']}"
    )
    print(f"  McNemar (不一致ペアのみ) p = {mcnemar_p(wins[label_a], wins[label_b]):.4f}")
    agree = dis = 0
    for r in results:
        if r["winner"] == "tie":
            continue
        va = r.get(label_a, {}).get("verified", 0)
        vb = r.get(label_b, {}).get("verified", 0)
        if va == vb:
            continue
        fewer = label_a if va < vb else label_b
        dis += 1
        agree += fewer == r["winner"]
    print(
        f"  ⭐ 独立採点の違反が少ない方が勝った: {agree}/{dis}"
        f"  (現行審判は 12/12 = 事後合理化の疑い)"
    )
    drop = sum(r.get(k, {}).get("dropped_unquoted", 0) for r in results for k in (label_a, label_b))
    claim = sum(r.get(k, {}).get("claimed", 0) for r in results for k in (label_a, label_b))
    print(f"  引用が本文に無く棄却した違反: {drop}/{claim}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--a", required=True, help="eval json:ラベル (基準腕)")
    p.add_argument("--b", required=True, help="eval json:ラベル (挑戦腕)")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--provider", default="ollama", choices=list(PROVIDERS))
    p.add_argument("--model", default="gemma3:12b")
    p.add_argument("--base-url", default="http://192.168.1.100:11434")
    p.add_argument("--out", default="data/mlx/judge_pbp.json")
    p.add_argument("--fresh", action="store_true")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
