#!/usr/bin/env python3
"""要点シートと審判の判定を強いモデル (既定 Opus) に点検させる — 人の確認の代わり (2026-09-26)。

松田ら (2026) と Norman ら (arXiv 2606.19544) が示すとおり、審判は安定していても正しいとは
限らない。利用者の判断で、人の確認の代わりに Opus を検証役に置く。点検は 2 つ:

1. 要点シート: 各要点が元記事に裏付けられているか・確度と大項目は正しいか・重複か、
   **抜けている重要な事実** (元記事からの逐語引用つき、引用が元記事に無ければ捨てる)
2. 審判の判定: 要点ごとの present / distortion が正しいか。誤りなら正しい値と理由

⚠ シートを作ったのも Opus なので、1 は自己点検になり甘くなりうる。指示で作成者と別の立場を
取らせ、抜けは逐語引用で裏付けさせる。2 は別モデル (Sonnet) の判定の点検なので独立性がある。

    uv run python scripts/review_keyfact_judgments.py --windows 9,13,22,18,33 \
        --arm data/mlx/eval_ollama_n17m30_v2.json:M30 --arm data/mlx/eval_ollama_n18.json:n18
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from scripts.judge_eventnews_pairwise import PROVIDERS, build_judge_client  # noqa: E402
from scripts.keyfact_lib import Distortion, norm, render_draft, source_block  # noqa: E402
from src.tools.llm_client import LLMClient, LLMError  # noqa: E402


class FactCheck(BaseModel):
    id: int
    verdict: Literal["妥当", "裏付けなし", "確度の誤り", "大項目の誤り", "重複", "重要度の誤り"]
    note: str = ""


class MissingFact(BaseModel):
    fact: str
    importance: Literal["核心", "補足"]
    quote: str = Field(description="元記事から一字一句そのまま")


class SheetReview(BaseModel):
    checks: list[FactCheck] = Field(default_factory=list)
    missing: list[MissingFact] = Field(default_factory=list)


class JudgmentCheck(BaseModel):
    id: int
    correct: bool = Field(description="審判の判定 (present と distortion の両方) が正しいか")
    right_present: bool
    right_distortion: Distortion
    note: str = ""


class JudgmentReview(BaseModel):
    checks: list[JudgmentCheck] = Field(default_factory=list)


_SYSTEM = (
    "あなたは CTI 記事の評価を監査する上級編集者です。他人が作った正解表と判定を、誤りを探す立場で"
    "点検します。与えられた記事と要約だけを根拠にし、自分の知識で補いません。"
)

_SHEET_TEMPLATE = """# 元記事
{sources}

# 要点シート (別の担当者が作成した正解表)
{facts}

# 指示
あなたは作成者ではありません。**誤りを探す立場で**点検してください。
- checks: すべての id について verdict を付ける。妥当 / 裏付けなし (元記事に書かれていない) /
  確度の誤り (確認済み・主張・報道・原文が留保 の付け方が違う) / 大項目の誤り /
  重複 (他の id と同じ事実) /
  重要度の誤り (核心と補足が逆)。妥当でないときは note に理由を 1 文
- missing: この記事群を 1 本のニュースにまとめるとき伝えるべきなのに、シートに無い事実。
  quote は元記事から **一字一句そのまま**。無ければ空配列 (件数を揃えない)"""

_JUDGE_TEMPLATE = """# 要点シート
{facts}

# 要約
{summary}

# 別の審判の判定
{judgments}

# 指示
別の審判が、要約が各要点を伝えているか (present) と、伝え方の誤り (distortion: なし / 内容の誤り /
数値の誤り / 確度の格上げ) を判定しました。**誤りを探す立場で**すべての id を点検してください。
- correct: 判定 (present と distortion の両方) が正しければ true
- right_present / right_distortion: あなたが正しいと考える値 (正しければ審判と同じ値)
- 確度の格上げ = 要点の確度が「主張・報道」「原文が留保」なのに要約が確定した事実として書いている
- 誤りのとき note に理由を 1 文"""


def _facts_text(facts: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"id={i}: [{f['category']}/{f['slot']}・確度 {f['certainty']}・{f['importance']}] "
        f"{f['fact']}"
        for i, f in enumerate(facts, start=1)
    )


def _judgments_text(rows: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"id={r['id']}: present={str(r['present']).lower()} distortion={r['distortion']}"
        + (f" (根拠: {r.get('quote', '')[:80]})" if r["present"] else "")
        for r in sorted(rows, key=lambda r: int(r["id"]))
    )


async def _call(client: LLMClient, prompt: str, schema: type[BaseModel]) -> Any:
    last: LLMError | None = None
    for _ in range(3):
        try:
            return await client.generate_structured(
                prompt=prompt, schema=schema, system=_SYSTEM, temperature=0.0, max_tokens=8000
            )
        except LLMError as e:
            last = e
            await asyncio.sleep(5)
    assert last is not None
    raise last


async def main_async(args: argparse.Namespace) -> int:
    windows = [int(w) for w in args.windows.split(",")]
    prompts = json.loads(Path(args.src).read_text(encoding="utf-8"))
    sheets = {
        int(s["index"]): s["facts"]
        for s in json.loads(Path(args.sheets).read_text(encoding="utf-8"))
    }
    coverage = json.loads(Path(args.coverage).read_text(encoding="utf-8"))
    client = build_judge_client(provider=args.provider, model=args.model, base_url=args.base_url)
    out_path = Path(args.out)
    out: dict[str, Any] = (
        json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
    )
    for w in windows:
        facts = sheets[w]
        sources = source_block(prompts[w]["prompt"])
        key = f"sheet:{w}"
        if key not in out:
            try:
                rev = await _call(
                    client,
                    _SHEET_TEMPLATE.format(sources=sources, facts=_facts_text(facts)),
                    SheetReview,
                )
            except LLMError as e:
                print(f"窓 {w} シート点検 ⚠ {str(e)[:120]}", flush=True)
                continue
            hay = norm(sources)
            missing = [m.model_dump() for m in rev.missing if m.quote and norm(m.quote) in hay]
            out[key] = {"checks": [c.model_dump() for c in rev.checks], "missing": missing}
            out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
            bad = sum(c.verdict != "妥当" for c in rev.checks)
            print(
                f"窓 {w} シート: 要点 {len(facts)} 中 指摘 {bad} / 抜け {len(missing)}", flush=True
            )
        for spec in args.arm:
            path, _, label = spec.partition(":")
            key = f"judge:{label}:{w}"
            rows = [r for r in coverage.get(label, []) if r["window"] == w]
            if key in out or not rows:
                continue
            summary = render_draft(
                json.loads(Path(path).read_text(encoding="utf-8"))[w].get("ollama_generated")
            )
            try:
                rev2 = await _call(
                    client,
                    _JUDGE_TEMPLATE.format(
                        facts=_facts_text(facts), summary=summary, judgments=_judgments_text(rows)
                    ),
                    JudgmentReview,
                )
            except LLMError as e:
                print(f"窓 {w} {label} 判定点検 ⚠ {str(e)[:120]}", flush=True)
                continue
            out[key] = [c.model_dump() for c in rev2.checks]
            out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
            ok = sum(c.correct for c in rev2.checks)
            print(f"窓 {w} {label} 判定: {ok}/{len(rev2.checks)} 正しい", flush=True)
    _report(out)
    return 0


def _report(out: dict[str, Any]) -> None:
    checks = [c for k, v in out.items() if k.startswith("sheet:") for c in v["checks"]]
    missing = [m for k, v in out.items() if k.startswith("sheet:") for m in v["missing"]]
    if checks:
        from collections import Counter

        print(f"\n=== シートの点検: 要点 {len(checks)} ===")
        print(f"  判定: {dict(Counter(c['verdict'] for c in checks))}")
        print(f"  抜け: {len(missing)} (核心 {sum(m['importance'] == '核心' for m in missing)})")
    judged = {k: v for k, v in out.items() if k.startswith("judge:")}
    for label in sorted({k.split(":")[1] for k in judged}):
        rows = [c for k, v in judged.items() if k.split(":")[1] == label for c in v]
        if rows:
            ok = sum(c["correct"] for c in rows)
            print(
                f"=== 審判 (Sonnet) の判定の正解率 [{label}]: "
                f"{ok}/{len(rows)} = {ok / len(rows):.1%}"
            )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--windows", required=True)
    p.add_argument("--arm", action="append", required=True, help="eval json:ラベル")
    p.add_argument("--src", default="data/mlx/eval_sft.json")
    p.add_argument("--sheets", default="data/mlx/keyfact_sheets.json")
    p.add_argument("--coverage", default="data/mlx/keyfact_coverage.json")
    p.add_argument("--out", default="data/mlx/keyfact_review.json")
    p.add_argument("--provider", default="claude-code", choices=list(PROVIDERS))
    p.add_argument("--model", default="opus")
    p.add_argument("--base-url", default="http://127.0.0.1:8010")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
