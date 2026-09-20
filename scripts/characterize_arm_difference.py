#!/usr/bin/env python3
"""2 腕の出力は「何が」違うのかを自由記述で書かせる (2026-09-20)。

⚠ **優劣を聞かない**。§58 で、同時採点の審判は結論を先に立てて観点を後から合わせる
(事後合理化) ことが実測で確認された — 接地違反が勝敗を 12/12 で完全予測していたのに、
独立採点にすると 5/11 (偶然水準) に落ちた。強制選択は同じ罠を踏む。

代わりに「差を記述させる」。⭐ **軸が分からなくなった状態で、軸を作り直すための測定**。
どちらが A でどちらが B かは伏せ、窓ごとに提示順を入れ替える。

    docker exec kuebiko python /app/scripts/characterize_arm_difference.py --n 6
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.tools.claude_code_client import ClaudeCodeClient  # noqa: E402

D = Path("/app/data/mlx") if Path("/app/data/mlx").exists() else _ROOT / "data" / "mlx"


class Difference(BaseModel):
    differences: list[str] = Field(
        default_factory=list, description="2 つの要約の具体的な違い。各 1 文。無ければ空"
    )
    same_quality: bool = Field(description="読み物として実質同等なら true")
    which_better: str = Field(description="X / Y / same のいずれか。判断できなければ same")
    why: str = Field(description="上の判断の理由 (2 文以内)")


_SYSTEM = (
    "あなたは CTI 記事要約の編集者です。入力に書かれている内容のみを根拠にしてください。"
    "違いが無いと感じたら、無理に違いを作らないでください。"
)

_TEMPLATE = """# 入力 (候補記事)
{prompt}

# 要約 X
{x}

# 要約 Y
{y}

# 指示
2 つの要約の**具体的な違い**を挙げてください。**無理に違いを探さないでください** —
実質的に同等なら `same_quality: true`、`differences: []` で構いません。
違いがある場合のみ、何がどう違うかを 1 件 1 文で書いてください。"""


async def main_async(args: argparse.Namespace) -> int:
    a = json.loads((D / "eval_ollama_n17m30.json").read_text(encoding="utf-8"))
    b = json.loads((D / "eval_ollama_n17c.json").read_text(encoding="utf-8"))
    # 同時採点と PBP で判定が割れた窓を優先する (情報量が最大)
    same_call = {
        int(r["index"]): r["winner"]
        for r in json.loads((D / "judge_M30_vs_n17c_gemma3-12b.json").read_text(encoding="utf-8"))
    }
    pbp = {
        int(r["index"]): r["winner"]
        for r in json.loads((D / "judge_pbp_M30_vs_n17c.json").read_text(encoding="utf-8"))
    }
    disagree = [i for i in sorted(same_call) if i in pbp and same_call[i] != pbp[i]]
    targets = (disagree or sorted(same_call))[: args.n]
    print(f"判定が割れた窓 {len(disagree)} 件 / 対象 {targets}", flush=True)

    client = ClaudeCodeClient(model=args.model, bridge_url=args.bridge_url, timeout_seconds=300)
    out: list[dict[str, Any]] = []
    out_path = D / "arm_difference_opus.json"
    if out_path.exists() and not args.fresh:
        out = json.loads(out_path.read_text(encoding="utf-8"))
    done = {r["index"] for r in out}

    for i in targets:
        if i in done:
            continue
        swap = i % 2 == 1  # 窓ごとに提示順を入れ替える
        x_lab, y_lab = ("n17c", "M30") if swap else ("M30", "n17c")
        x = (b if swap else a)[i].get("ollama_generated") or ""
        y = (a if swap else b)[i].get("ollama_generated") or ""
        if not x or not y:
            continue
        r = await client.generate_structured(
            prompt=_TEMPLATE.format(prompt=a[i]["prompt"], x=x, y=y),
            schema=Difference,
            system=_SYSTEM,
            temperature=0.0,
            max_tokens=1500,
        )
        better = {"X": x_lab, "Y": y_lab}.get(r.which_better.strip().upper(), "same")
        out.append(
            {
                "index": i,
                "x_is": x_lab,
                "same_quality": r.same_quality,
                "better": better,
                "why": r.why,
                "differences": r.differences,
            }
        )
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(
            f"  窓 {i:3d}: 同等 {r.same_quality} / 優 {better} / 違い {len(r.differences)} 件",
            flush=True,
        )
        for d in r.differences[:3]:
            print(f"      - {d[:110]}", flush=True)

    same = sum(1 for r in out if r["same_quality"])
    print(
        f"\n集計 {len(out)} 件: 実質同等と判定 {same} / "
        f"M30 優 {sum(1 for r in out if r['better'] == 'M30')} / "
        f"n17c 優 {sum(1 for r in out if r['better'] == 'n17c')}"
    )
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--n", type=int, default=6)
    p.add_argument("--model", default="opus")
    p.add_argument("--bridge-url", default="http://claude-bridge:8010")
    p.add_argument("--fresh", action="store_true")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
