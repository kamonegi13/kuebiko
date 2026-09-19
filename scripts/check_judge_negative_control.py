#!/usr/bin/env python3
"""採点者を使う前に検出力を確かめる (2026-09-19)。

⚠ **効かない採点者を信じない**。2026-09-19 に接地検証器を作ったとき、最初の実装は全件
「支えている」と答えた。負のコントロール (捏造・推論の断定・記事にない数値を仕込んだ既知の
事例) を通して初めて、検証器ではなく**対象欄の絞りすぎ**が原因と分かった。

同じ入力から、既知の欠陥を仕込んだ要約 B と、素直な要約 A を作り、採点者が B を劣ると
判定できるかを見る。**判定できなければ、その採点者で腕を比べても意味がない**。

    docker exec kuebiko python /app/scripts/check_judge_negative_control.py \
        --provider ollama --base-url http://192.168.1.100:11434 --model gemma3:12b
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from scripts.judge_eventnews_pairwise import (  # noqa: E402
    PROVIDERS,
    _judge_one,
    build_judge_client,
)

_INPUT = """[1] **Chrome の脆弱性 3 件を修正** (Example Security, 2026-09-10T00:00:00+00:00)
Google は Chrome 151.0.7922.138 で 3 件の脆弱性を修正した。いずれも外部研究者からの報告で、
悪用は確認されていない。修正版は Windows と macOS 向けに順次配信される。

[2] **Chrome 更新の配信開始** (Example Wire, 2026-09-10T02:00:00+00:00)
Chrome の更新が配信された。対象は 151.0.7922.138。開発元は利用者に速やかな適用を促している。
"""

_CLEAN = json.dumps(
    {
        "headline": "Chrome 151.0.7922.138 で脆弱性 3 件を修正、悪用は未確認",
        "bluf": (
            "Google は Chrome 151.0.7922.138 で 3 件の脆弱性を修正した。悪用は確認されていない。"
        ),
        "key_points": ["修正版は 151.0.7922.138", "悪用は確認されていない"],
        "facts": [
            {
                "text": "Google は Chrome 151.0.7922.138 で 3 件の脆弱性を修正した。",
                "source_index": 1,
            },
            {"text": "いずれも外部研究者からの報告である。", "source_index": 1},
        ],
        "discrepancies": [],
        "caveats": [
            {
                "text": "悪用が確認されていないことは、悪用が不可能であることを意味しない。",
                "source_index": 1,
            }
        ],
        "unknowns": ["脆弱性の技術的詳細は公表されていない"],
    },
    ensure_ascii=False,
)

# 既知の欠陥を 4 種仕込む: 版数の取り違え / 捏造 / 偽の対比 / 記事にない数値
_FLAWED = json.dumps(
    {
        "headline": "Chrome 151.0.79222.138 で脆弱性 3 件を修正、中国系 APT が悪用",
        "bluf": (
            "Google は Chrome 151.0.79222.138 で 3 件の脆弱性を修正した。"
            "中国系 APT による悪用が続いている。"
        ),
        "key_points": ["修正版は 151.0.79222.138", "影響利用者は 2,600 万人"],
        "facts": [
            {
                "text": "Google は Chrome 151.0.79222.138 で 3 件の脆弱性を修正した。",
                "source_index": 1,
            },
            {"text": "本脆弱性は中国系 APT によって 8 月から悪用されている。", "source_index": 1},
            {"text": "影響を受けた利用者は 2,600 万人にのぼる。", "source_index": 2},
        ],
        "discrepancies": [
            {
                "text": "記事 [1] は 151.0.7922.138、記事 [2] は 151.0.7922.138 と版数が食い違う。",
                "source_index": 1,
            }
        ],
        "caveats": [],
        "unknowns": [],
    },
    ensure_ascii=False,
)


async def main_async(args: argparse.Namespace) -> int:
    client = build_judge_client(provider=args.provider, model=args.model, base_url=args.base_url)
    print(f"採点者: {args.provider} / {args.model}\n")
    ok = 0
    for i, (a, b, expect) in enumerate(
        ((_CLEAN, _FLAWED, "A"), (_FLAWED, _CLEAN, "B")),
        1,  # 提示順を入れ替えて 2 回
    ):
        v = await _judge_one(client, _INPUT, a, b)
        hit = v.winner == expect
        ok += hit
        good = v.a if expect == "A" else v.b
        bad = v.b if expect == "A" else v.a
        print(
            f"  パス {i}: 勝者 {v.winner} (期待 {expect}) {'✓' if hit else '✗'}  "
            f"接地違反 素直 {good.grounding_violations} / 欠陥 {bad.grounding_violations}"
        )
        print(f"    理由: {v.reason[:130]}")
    print(
        f"\n合否: {ok}/2 "
        + ("合格 — この採点者は欠陥を見分けられる" if ok == 2 else "不合格 — 腕の比較に使えない")
    )
    return 0 if ok == 2 else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--provider", default="ollama", choices=list(PROVIDERS))
    p.add_argument("--model", default="gemma3:12b")
    p.add_argument("--base-url", default="http://192.168.1.100:11434")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
