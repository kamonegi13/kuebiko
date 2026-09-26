#!/usr/bin/env python3
"""要点シートで事象ニュースの欠落と誤変換を測る (2026-09-26、設計は scripts/keyfact_lib.py)。

各腕の出力を **単独で** 要点シートと照合する (相手を見ない = 一対比較の位置・長さの偏りを
受けにくい)。1 窓 1 呼出で全要点を 2 値判定し、誤変換は別欄で種類ごとに数える。
「含む」には要約からの逐語引用を必須とし、引用が要約に無ければ「含まない」に倒す。

    uv run python scripts/judge_keyfact_coverage.py --sheets data/mlx/keyfact_sheets.json \
        --arm data/mlx/eval_ollama_n17m30_v2.json:M30 --arm data/mlx/eval_ollama_n18.json:n18 \
        --provider claude-code --model sonnet --base-url http://127.0.0.1:8010
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from scripts.judge_eventnews_pairwise import PROVIDERS, build_judge_client  # noqa: E402
from scripts.keyfact_lib import CoverageResult, mcnemar_p, render_draft, settle  # noqa: E402
from src.tools.llm_client import LLMClient, LLMError  # noqa: E402

_SYSTEM = (
    "あなたは CTI 記事要約の事実確認者です。要点一覧の各事実が、要約に書かれているかを 1 件ずつ"
    "確かめます。要約に書かれている文言だけを根拠にし、推測で補いません。"
)

_TEMPLATE = """# 要点一覧 (正解表)
{facts}

# 点検する要約
{summary}

# 指示
要点一覧の **すべての id** について 1 件ずつ判定し、id の順に返してください。
- present: 要約がその事実を伝えていれば true (言い換え・一部でも、事実の中身が伝わっていれば true)。
  伝えていなければ false
- quote: present が true のとき、根拠になる要約の箇所を **一字一句そのまま** 引用。false なら空文字
- distortion: present が true のとき、伝え方に誤りがあるか
  - なし
  - 内容の誤り: 主体・対象・出来事が違う (**対象のすり替えを含む**。
    例: 「米国の資産」→「湾岸諸国の資産」)
  - 数値の誤り: 件数・金額・日付・版が違う
  - 確度の格上げ: 要点の確度が「主張・報道」「原文が留保」なのに、
    要約が確定した事実として書いている。
    **要約中で最も強い書き方で判定する** — 事実欄に留保があっても、見出し・BLUF・要点で言い切って
    いれば格上げ (読者が最初に読むのはそこ)
  - 確度の格下げ: 要点の確度が「確認済み」なのに、要約が「不明」「未検証」
    「〜かどうか分からない」と書いている
  - present が false なら「なし」
- 要点一覧に無い事実は判定しない。id を飛ばさない・作らない"""


def _facts_text(facts: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"id={i}: [{f['category']}/{f['slot']}・確度 {f['certainty']}] {f['fact']}"
        for i, f in enumerate(facts, start=1)
    )


async def _judge(client: LLMClient, facts: list[dict[str, Any]], summary: str) -> CoverageResult:
    last: LLMError | None = None
    for _ in range(3):
        try:
            return await client.generate_structured(
                prompt=_TEMPLATE.format(facts=_facts_text(facts), summary=summary),
                schema=CoverageResult,
                system=_SYSTEM,
                temperature=0.0,
                max_tokens=6000,
            )
        except LLMError as e:
            last = e
            await asyncio.sleep(5)
    assert last is not None
    raise last


def _summarize(
    label: str, rows: list[dict[str, Any]], sheets: dict[int, list[dict[str, Any]]]
) -> None:
    core = [r for r in rows if sheets[r["window"]][r["id"] - 1]["importance"] == "核心"]
    for name, sel in (("全要点", rows), ("核心", core)):
        if not sel:
            continue
        cov = sum(r["present"] for r in sel) / len(sel)
        dist = Counter(r["distortion"] for r in sel if r["present"])
        print(
            f"  [{label}] {name}: 被覆 {cov:.1%} ({sum(r['present'] for r in sel)}/{len(sel)})"
            f" / 誤変換 {sum(v for k, v in dist.items() if k != 'なし')} {dict(dist)}"
        )
    flags = Counter(r["flag"] for r in rows if r["flag"])
    if flags:
        print(
            f"  [{label}] 引用不在で含まないに倒した {flags.get('unverified', 0)}"
            f" / 審判が返さなかった {flags.get('judge_missing', 0)}"
        )
    by_cat: dict[str, list[bool]] = {}
    for r in rows:
        by_cat.setdefault(sheets[r["window"]][r["id"] - 1]["category"], []).append(r["present"])
    print(
        f"  [{label}] 大項目別の被覆: "
        + ", ".join(f"{k} {sum(v) / len(v):.0%} (n={len(v)})" for k, v in sorted(by_cat.items()))
    )


async def main_async(args: argparse.Namespace) -> int:
    sheets_raw = json.loads(Path(args.sheets).read_text(encoding="utf-8"))
    sheets = {int(s["index"]): s["facts"] for s in sheets_raw if s["facts"]}
    if args.windows:  # 審判の検証など、一部の窓だけを照合する
        keep = {int(w) for w in args.windows.split(",")}
        sheets = {w: f for w, f in sheets.items() if w in keep}
    client = build_judge_client(provider=args.provider, model=args.model, base_url=args.base_url)
    out_path = Path(args.out)
    results: dict[str, list[dict[str, Any]]] = (
        json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
    )
    for spec in args.arm:
        # path:ラベル[:欄名] — 欄名の既定は ollama_generated (教師の出力は reference)
        path, _, rest = spec.partition(":")
        label, _, field = rest.partition(":")
        label = label or Path(path).stem
        field = field or "ollama_generated"
        outputs = json.loads(Path(path).read_text(encoding="utf-8"))
        rows = results.setdefault(label, [])
        done = {r["window"] for r in rows}
        for w, facts in sorted(sheets.items()):
            if w in done or w >= len(outputs):
                continue
            summary = render_draft(outputs[w].get(field))
            if not summary:
                # 生成失敗は全要点を欠落として数える (JSON 崩れも読者に届かない)
                settled = settle([], len(facts), "")
            else:
                try:
                    res = await _judge(client, facts, summary)
                except LLMError as e:
                    print(f"{label} {w:3d} ⚠ 審判失敗 {type(e).__name__} — 飛ばす", flush=True)
                    continue
                settled = settle(list(res.judgments), len(facts), summary)
            rows += [{"window": w, **r} for r in settled]
            out_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
            hit = sum(r["present"] for r in settled)
            print(f"{label} {w:3d} 被覆 {hit}/{len(facts)}", flush=True)

    print("\n=== 集計 ===")
    for label, rows in results.items():
        _summarize(label, rows, sheets)
    labels = list(results)
    if len(labels) >= 2:
        a, b = labels[0], labels[1]
        ka = {(r["window"], r["id"]): r["present"] for r in results[a]}
        kb = {(r["window"], r["id"]): r["present"] for r in results[b]}
        common = [k for k in ka if k in kb and sheets[k[0]][k[1] - 1]["importance"] == "核心"]
        a_only = sum(ka[k] and not kb[k] for k in common)
        b_only = sum(kb[k] and not ka[k] for k in common)
        print(
            f"\n核心要点の対応比較 ({len(common)} 件): "
            f"{a} だけ含む {a_only} / {b} だけ含む {b_only}"
            f" / McNemar p = {mcnemar_p(a_only, b_only):.4f}"
        )
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--sheets", default="data/mlx/keyfact_sheets.json")
    p.add_argument("--arm", action="append", required=True, help="eval json:ラベル[:欄名] (複数可)")
    p.add_argument("--out", default="data/mlx/keyfact_coverage.json")
    p.add_argument("--windows", default="", help="照合する窓番号 (カンマ区切り、空 = 全部)")
    p.add_argument("--provider", default="claude-code", choices=list(PROVIDERS))
    p.add_argument("--model", default="sonnet")
    p.add_argument("--base-url", default="http://127.0.0.1:8010")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
