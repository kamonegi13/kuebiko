#!/usr/bin/env python3
"""事象ニュースの凍結評価窓ごとに要点シートを作る (2026-09-26、設計は scripts/keyfact_lib.py)。

元記事 (prompt 内の「対象記事」) だけから、記事群が伝えている事実を原子的な要点に分けて
列挙させる。各要点は大項目・小項目・確度・重要度と **元記事からの逐語引用** を持ち、
引用が元記事に実在しない要点は捨てる (作り話の要点で被覆率が歪むのを防ぐ)。

⚠ シートの品質が評価全体を左右する (松田ら 2026)。下書きは強いモデル (既定 Opus) に書かせ、
数窓を人が点検してから使う。8B 級に書かせると要点の 15% しか拾えなかった (同論文 §5)。

    uv run python scripts/build_keyfact_sheets.py --provider claude-code --model opus \
        --base-url http://127.0.0.1:8010 --out data/mlx/keyfact_sheets.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from scripts.judge_eventnews_pairwise import PROVIDERS, build_judge_client  # noqa: E402
from scripts.keyfact_lib import KeyFactSheet, source_block, verified_facts  # noqa: E402
from src.tools.llm_client import LLMClient, LLMError  # noqa: E402

_SYSTEM = (
    "あなたは CTI (サイバー脅威インテリジェンス) の編集者です。与えられた記事群だけを根拠にし、"
    "自分の知識で補いません。記事群が伝えている事実を、評価用の要点シートとして列挙します。"
)

_TEMPLATE = """# 記事群
{sources}

# 指示
この記事群を 1 本のニュースにまとめるとき、**読者に伝えるべき事実**を要点として列挙してください。
この一覧は、要約が必要な情報を落としていないかを確かめるための正解表になります。

- **1 要点 = 1 事実** (原子的に)。「A 社が B を公表し C 件が流出」は 2〜3 要点に分ける
- category: 前提条件 (背景・経緯) / 攻撃 (攻撃者・手口) / 脆弱性 (脆弱性・影響製品・修正) /
  被害 (被害組織・規模・影響) / 発覚 (発覚の経緯・公表) / 調査 (調査の状況・結果) /
  対策 (対応・緩和・推奨)
- slot: 誰が / 誰の / 誰に / 何をした / いつ / どこで / なぜ / どうやって / どれくらい / その他
- certainty: 確認済み (被害組織・ベンダ・当局が確認) / 主張・報道 (攻撃者の主張、1 媒体の報道、
  「〜とされる」) / 原文が留保 (原文が自ら限定・留保している事柄)
- importance: 核心 (欠けるとニュースとして誤解を招く・用をなさない) / 補足 (あれば望ましい)
- **原文が付けた但し書き・留保も要点にする** (例: 「サンプルは公開されておらず主張は未検証」)
  — 要約で最初に落ちる種類の情報で、落ちると読み手が誤読する
- quote は根拠の記事本文から **一字一句そのまま** 引用する (言い換えない・訳さない)。
  source_index はその記事の番号 [N]
- **必ず要点にするもの** (09-26 の点検で抜けていた種類):
  - 脆弱性は **CVE ごとに** 種類 (RCE・範囲外読み取り・認証回避 等) と
    影響 (何ができるか) を 1 要点ずつ
  - 主要な当事者 (被害組織・ベンダ・当局・国家・攻撃者) の **公式の立場・声明の要旨**
- 記事に書かれていないことは書かない。**同じ事実を言い換えて重複させない** (別の大項目でも 1 回だけ)
- 数はこの記事群の情報量に合わせる (目安 5〜20)。件数を水増ししない"""


async def _draft(client: LLMClient, prompt: str) -> KeyFactSheet:
    last: LLMError | None = None
    for _ in range(3):
        try:
            return await client.generate_structured(
                prompt=_TEMPLATE.format(sources=source_block(prompt)),
                schema=KeyFactSheet,
                system=_SYSTEM,
                temperature=0.0,
                max_tokens=6000,
            )
        except LLMError as e:
            last = e
            await asyncio.sleep(5)
    assert last is not None
    raise last


async def main_async(args: argparse.Namespace) -> int:
    records: list[dict[str, Any]] = json.loads(Path(args.src).read_text(encoding="utf-8"))
    n = min(len(records), args.limit or len(records))
    out_path = Path(args.out)
    sheets: list[dict[str, Any]] = (
        json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else []
    )
    done = {s["index"] for s in sheets}
    client = build_judge_client(provider=args.provider, model=args.model, base_url=args.base_url)
    for i in range(n):
        if i in done:
            continue
        prompt = records[i]["prompt"]
        try:
            sheet = await _draft(client, prompt)
        except LLMError as e:
            print(f"{i:3d} ⚠ 失敗 {type(e).__name__} — 飛ばす", flush=True)
            continue
        kept, dropped = verified_facts(sheet.facts, source_block(prompt))
        sheets.append(
            {
                "index": i,
                "facts": [f.model_dump() for f in kept],
                "dropped_unquoted": dropped,
                "model": args.model,
            }
        )
        sheets.sort(key=lambda s: int(s["index"]))
        out_path.write_text(json.dumps(sheets, ensure_ascii=False, indent=1), encoding="utf-8")
        core = sum(f.importance == "核心" for f in kept)
        print(f"{i:3d} 要点 {len(kept)} (核心 {core}) / 引用不在で棄却 {dropped}", flush=True)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--src", default="data/mlx/eval_sft.json")
    p.add_argument("--out", default="data/mlx/keyfact_sheets.json")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--provider", default="claude-code", choices=list(PROVIDERS))
    p.add_argument("--model", default="opus")
    p.add_argument("--base-url", default="http://127.0.0.1:8010")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
