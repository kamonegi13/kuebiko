#!/usr/bin/env python3
"""事象ニュースの接地検証を「作る前に」測る (2026-09-19、SYNTHESIS §53 の接地違反)。

審判は n17c の接地違反を n17m30 の 2 倍と判定した (0.94 → 1.78、13 窓中 12 窓、p=0.003)。
だが決定論で測れる信号はどれも腕を分けなかった:

| 信号 | n17m30 | n17c |
|---|---|---|
| 識別子の取り違え (長い値の 1 文字違い) | 3 腕 116 件で合計 1 件 | |
| 引用先の記事に無い識別子 (帰属違い) | 4.1% | **2.8%** (むしろ少ない) |
| カタログに無い version/cvss | 8/25 | 10/24 |

⭐ **差は意味の層にある** — 識別子も数値も正しいまま、記事が支えていない主張を書いている。
そこを見るには LLM に照合させるしかない。**作る前に、その検証が審判と同じものを見るかを測る**
(効かない関門を本番に入れない — 08-22 の引用関門は測ってから入れた)。

    OLLAMA_BASE_URL=http://127.0.0.1:11434 \
        uv run python scripts/measure_grounding_verifier.py --limit 16
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from pydantic import BaseModel, Field  # noqa: E402

from src.config_loader import load_app_config  # noqa: E402
from src.tools.llm_client import OllamaClient  # noqa: E402

D = _ROOT / "data" / "mlx"
ARMS = {"M30": D / "eval_ollama_n17m30.json", "n17c": D / "eval_ollama_n17c.json"}
JUDGE = D / "judge_M30_vs_n17c.json"
_BODY = re.compile(r"^\s*\[(\d+)\]\s*(.*?)(?=^\s*\[\d+\]|\Z)", re.M | re.S)
_MODEL = "kuebiko-sft:s17"
_BODY_CHARS = 6000


class _Verdict(BaseModel):
    index: int = Field(description="判定対象の行番号 (1-based)")
    supported: bool = Field(description="引用した記事がその主張を支えているか")
    reason: str = Field(description="支えていない場合の理由 (1 文、支えていれば空)")


class _Out(BaseModel):
    verdicts: list[_Verdict] = Field(default_factory=list)


def bodies_of(prompt: str) -> dict[int, str]:
    out: dict[int, str] = {}
    for n, t in _BODY.findall(prompt):
        out[int(n)] = out.get(int(n), "") + " " + t
    return out


def build_prompt(facts: list[dict[str, object]], bodies: dict[int, str]) -> str:
    """各行と、その行が引用した記事だけを並べる (他の記事は見せない)。"""
    lines = []
    for i, f in enumerate(facts, 1):
        n = int(str(f.get("source_index") or 0))
        # 出典番号 0 = 要約層。特定の記事に紐づかないので全記事を並べて照合する
        body = (
            (bodies.get(n) or "")[:_BODY_CHARS]
            if n
            else "\n".join(
                f"[{k}] {v[: _BODY_CHARS // max(1, len(bodies))]}"
                for k, v in sorted(bodies.items())
            )
        )
        kind = {
            "facts": "事実",
            "discrepancies": "媒体間の相違",
            "caveats": "留保",
            "headline": "見出し",
            "bluf": "要旨",
            "key_points": "要点",
        }.get(str(f.get("field") or "facts"), "事実")
        cite = f"引用記事 [{n}]" if n else "出典指定なし (全記事が対象)"
        lines.append(f"### 行 {i} ({kind}、{cite})\n主張: {f.get('text', '')}\n記事本文:\n{body}")
    return (
        "以下の各行について、**引用した記事本文がその主張を支えているか**を判定してください。\n"
        "支えていないと判定するのは次の場合です: 記事に書かれていない事実を述べている / "
        "記事の記述から推論した含意を事実として述べている / 記事が留保している事柄を断定している / "
        "記事にない対比や因果を作っている。\n"
        "**言い換えや要約は支えていると判定します**。\n"
        "「媒体間の相違」の行は、**引用記事が実際に他と食い違う記述をしているか**で判定します "
        "(同じ値を別の言い回しで書いているだけなら支えていません)。\n"
        "判定は行ごとに独立に行ってください。\n\n" + "\n\n".join(lines)
    )


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=16, help="対象の窓数 (審判済みの窓から)")
    ap.add_argument("--model", default=_MODEL)
    ap.add_argument("--out", type=Path, default=D / "grounding_verifier_measure.json")
    args = ap.parse_args()

    judged = [int(r["index"]) for r in json.loads(JUDGE.read_text(encoding="utf-8"))][: args.limit]
    gens = {k: json.loads(p.read_text(encoding="utf-8")) for k, p in ARMS.items()}
    cfg = load_app_config()
    llm = OllamaClient(base_url=cfg.ollama_base_url, model=args.model, timeout_seconds=900.0)

    done: dict[str, dict[str, object]] = {}
    if args.out.exists():  # 再開可 (呼んだ後の出力は捨てない)
        done = {str(d["key"]): d for d in json.loads(args.out.read_text(encoding="utf-8"))}

    for idx in judged:
        for arm in ARMS:
            key = f"{idx}:{arm}"
            if key in done:
                continue
            rec = gens[arm][idx]
            bodies = bodies_of(rec.get("prompt") or "")
            try:
                o = json.loads(rec.get("ollama_generated") or rec.get("generated") or "")
            except Exception:  # noqa: BLE001 — 壊れた出力は測れないので飛ばす
                continue
            # ⚠ facts だけを見ると 0 件になる (2026-09-19 実測)。審判が挙げた接地違反の
            #    多くは **相違欄の偽の対比** と **留保に付けた解釈**だった。欄を絞ると
            #    「関門は通ったが何も見ていない」になる。
            facts = [
                {**f, "field": field}
                for field in ("facts", "discrepancies", "caveats")
                for f in (o.get(field) or [])
                if isinstance(f, dict)
            ]
            # 要約層 (見出し・BLUF・要点) は出典番号を持たないので、全記事を照合対象にする。
            # ⚠ ここを外すと審判が見た範囲と揃わない (2026-09-19: 3 欄だけで測って
            #    「腕の差が無い」と読みかけた)。
            facts += [
                {"text": str(t), "source_index": 0, "field": field}
                for field, vals in (
                    ("headline", [o.get("headline") or ""]),
                    ("bluf", [o.get("bluf") or ""]),
                    ("key_points", list(o.get("key_points") or [])),
                )
                for t in vals
                if str(t).strip()
            ]
            if not facts:
                continue
            try:
                out = await llm.generate_structured(
                    build_prompt(facts, bodies),
                    schema=_Out,
                    temperature=0.0,
                    max_tokens=4096,
                    think=False,
                )
                # ⚠ **範囲外の行番号を数えない** (2026-09-19)。1 窓で 23 件中 13 件が
                #    存在しない行への判定で、それだけで腕の優劣が逆転していた。
                #    番号が壊れた出力は「判定できなかった」として別に数える。
                bad = [v for v in out.verdicts if not v.supported and 1 <= v.index <= len(facts)]
                invalid = sum(1 for v in out.verdicts if not 1 <= v.index <= len(facts))
            except Exception as exc:  # noqa: BLE001 — 個別失敗は記録して続行
                print(f"  {key}: 失敗 {type(exc).__name__}", flush=True)
                continue
            done[key] = {
                "key": key,
                "index": idx,
                "arm": arm,
                "facts": len(facts),
                "by_field": {
                    k: sum(1 for f in facts if f.get("field") == k)
                    for k in ("facts", "discrepancies", "caveats", "headline", "bluf", "key_points")
                },
                "unsupported_fields": [
                    str(facts[v.index - 1].get("field")) for v in bad if 1 <= v.index <= len(facts)
                ],
                "unsupported": len(bad),
                "invalid_index": invalid,
                "reasons": [v.reason[:160] for v in bad][:6],
            }
            print(
                f"  {key}: facts {len(facts)} / 支えなし {len(bad)}"
                + (f" ⚠範囲外 {invalid}" if invalid else ""),
                flush=True,
            )
            args.out.write_text(
                json.dumps(list(done.values()), ensure_ascii=False, indent=1), encoding="utf-8"
            )
    print(f"書込: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
