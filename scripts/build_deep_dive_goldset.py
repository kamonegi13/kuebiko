#!/usr/bin/env python3
"""深掘り選定の凍結審判を**現行非依存**で作る (2026-09-21)。

なぜ要るか: 深掘りには独立した正解が 1 件も無く、測れるのは「LLM と同じものを選ぶか」
だけだった。そのため今日 2 つの問いが判定不能で止まった:

- ML 単独の品質は LLM より劣るのか (前段 top-90 は測れたが、単独の可否は不明)
- Sonnet と n17m30 のどちらが良い選抜か (一致 45%、自己一致は**どちらも 70%**)

⚠ 現行の rubric (pir / roi) を審判の軸に流用してはいけない。それは現行の採点軸で
あって正解の基準ではない (2026-09-04「現状を真値に置くバイアス」)。

### 軸 — 深掘りの目的をそのまま問う (2026-09-21、利用者の定義)

> **Alert / Brief には上がってこない速報性のないものであるが、CTI 担当者として
> 把握しておく必要のある内容を深掘りして出す**

これは 2 つの独立した問いで、**掛け合わせ**が深掘りの対象になる:

- ``urgency``      0-3 速報 (alert/brief) で即日伝えるべき緊急性。**高いほど深掘りの対象外**
- ``need_to_know`` 0-3 CTI 担当者として把握しておく必要があるか

    深掘りの対象 = urgency 低 × need_to_know 高

⚠ 初版は mission / insight / standalone / worth_reading を聞いていたが、
**「速報性がない」という条件を測れていなかった**。32 件を採点した時点で破棄した。
軸を現行 rubric (pir/roi) から取らないことばかり気にして、**目的の定義そのものを
軸にする**という最も素直な設計を外していた。

補助の軸:
- ``depth``  0-3 深掘りに耐える中身があるか (一次情報・構造的分析か、事実の再掲か)

⚠ 速報の全文は見せない (detect の審判で「能動情勢のタイトルを毎回載せると CoT 収穫より
高くつく」として避けたのと同じ)。記事自体の性質から緊急性を判定させる。

⚠ 層ラベル (現行が選んだか等) は**審判に見せない** (盲検)。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.config_loader import load_app_config  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.tools.llm_client import LLMClient, LLMError  # noqa: E402
from src.tools.model_tiers import Step, build_llm_for_ref  # noqa: E402

LABELS = Path("data/mlx/deep_dive_labels.jsonl")
OUT = Path("data/mlx/deep_dive_goldset.jsonl")
_SEED = 20260921
#: 層ごとの上限。現行が選んだ分だけを審判すると「現行が正しい」しか測れないので、
#: **落選分と無作為**を必ず混ぜる (detect の審判と同じ 3 層の思想)。
#: ⚠ 139 件では ML の学習に足りない (detect は 1,075 件、特徴量は 54 列)。
#: さらに need_to_know=2 に 65% が集中しており、中間値に寄った分布では
#: 「必要度」を学習できない。**層ごとに増やして分布を広げる**。
_PER_STRATUM = {"selected": 150, "near_miss": 120, "random": 180}


class _Verdict(BaseModel):
    """審判の判定。**緊急性と把握必要性を分けて持つ**のが肝。

    深掘りの対象は「速報で扱うほど急がないが、把握しておく必要がある」もの。
    1 つの軸 (読む価値) に潰すと、**急ぐから読むべき**ものと**急がないが要る**ものが
    区別できず、深掘りが alert/brief と重複しているかを判定できない。
    """

    model_config = ConfigDict(extra="ignore")

    #: 0-3 速報で即日伝えるべき緊急性。**高いほど深掘りの対象外** (alert/brief の仕事)
    urgency: int = 0
    #: 0-3 CTI 担当者として把握しておく必要があるか
    need_to_know: int = 0
    #: 0-3 深掘りに耐える中身があるか (一次情報・構造的分析か、事実の再掲か)
    depth: int = 0
    reason: str = ""
    confidence: str = "low"  # high | moderate | low


_SYSTEM = (
    "あなたは日本の CTI アナリストです。日々の速報 (alert / brief) とは別に、"
    "週末にまとめて深掘りする記事を選びます。"
    "与えられた記事の見出しと要約だけで判断し、自分の知識で補わないでください。"
)

_TEMPLATE = """# 記事
題名: {title}
媒体: {feed}
要約: {summary}

# 判定してほしいこと

この記事を **3 つの軸**で独立に採点してください。互いに引きずられないでください。

1. **urgency** (0-3) — **速報で即日伝えるべき緊急性**。
   3 = 今日中に知らせないと対処が間に合わない (実悪用中の重大脆弱性・進行中の国内侵害)
   2 = 数日以内に知らせるべき
   1 = 急がないが期限はある
   0 = 急ぐ理由がない (分析・解説・振り返り・長期的な動向)

2. **need_to_know** (0-3) — **日本の CTI 担当者として把握しておく必要**があるか。
   緊急かどうかとは**切り離して**評価する。
   3 = 知らないと任務に支障がある / 2 = 知っておくべき / 1 = 知っていれば役に立つ / 0 = 不要

3. **depth** (0-3) — 深掘りに耐える中身があるか。
   3 = 一次情報・新規 TTP・構造的分析 / 2 = 既知の事案に新しい視点
   1 = 事実の再掲のみ / 0 = 中身が無い

⚠ **urgency が高いものは深掘りの対象ではありません** (速報の仕事です)。
それでも urgency は正直に付けてください — 判定はこちらで行います。

理由を 1-2 文で述べ、確信度を high / moderate / low で示してください。"""


def select_population(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """3 層の母集団。層ラベルは集計用で**審判には見せない**。"""
    by_win: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r.get("composite") is not None:
            by_win[str(r["window_end"])].append(r)
    selected: list[dict[str, Any]] = []
    near: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    for rs in by_win.values():
        ranked = sorted(rs, key=lambda r: -float(r["composite"]))
        selected.extend(ranked[:20])
        near.extend(ranked[20:40])  # ⭐ 落選のすぐ外側 — ここが判定の境目
        rest.extend(ranked[40:])
    rnd = random.Random(_SEED)
    rnd.shuffle(selected)
    rnd.shuffle(near)
    rnd.shuffle(rest)
    out: list[dict[str, Any]] = []
    for label, pool in (("selected", selected), ("near_miss", near), ("random", rest)):
        for r in pool[: _PER_STRATUM[label]]:
            out.append({**r, "stratum": label})
    return out


async def judge(llm: LLMClient, art: dict[str, Any]) -> _Verdict | None:
    prompt = _TEMPLATE.format(
        title=art.get("title", ""), feed=art.get("feed_title", ""), summary=art.get("summary", "")
    )
    for _ in range(3):
        try:
            return await llm.generate_structured(
                prompt=prompt, schema=_Verdict, system=_SYSTEM, temperature=0.0, max_tokens=700
            )
        except LLMError:
            await asyncio.sleep(5)
    return None


async def main_async(args: argparse.Namespace) -> int:
    rows = [json.loads(x) for x in LABELS.read_text(encoding="utf-8").splitlines() if x.strip()]
    if args.ids_file:
        want = {
            x.strip()
            for x in Path(args.ids_file).read_text(encoding="utf-8").splitlines()
            if x.strip()
        }
        pop = [{**r, "stratum": "targeted"} for r in rows if r["article_id"] in want]
    else:
        pop = select_population(rows)
    repo = RunHistoryRepository()
    ids = [r["article_id"] for r in pop]
    texts: dict[str, dict[str, Any]] = {}
    with repo._connect() as conn:  # noqa: SLF001 — 読み取り専用の接続 seam 共有
        for i in range(0, len(ids), 400):
            chunk = ids[i : i + 400]
            ph = ",".join("?" * len(chunk))
            for row in conn.execute(
                "SELECT article_id, title, feed_title, summary "  # noqa: S608
                f"FROM articles WHERE article_id IN ({ph})",
                tuple(chunk),
            ).fetchall():
                texts[str(row["article_id"])] = dict(row)
    seen = {
        json.loads(x)["article_id"]
        for x in (OUT.read_text(encoding="utf-8").splitlines() if OUT.exists() else [])
        if x.strip()
    }
    todo = [r for r in pop if r["article_id"] in texts and r["article_id"] not in seen]
    strata: defaultdict[str, int] = defaultdict(int)
    for r in pop:
        strata[r["stratum"]] += 1
    print(f"母集団 {len(pop)} 件 {dict(strata)} / 本文あり {len(texts)} / 未判定 {len(todo)}")
    if not args.apply:
        print("\n(dry-run — 外部枠を使わない。実行は --model <ref> --apply)")
        return 0

    llm = build_llm_for_ref(args.model, Step.DIGEST_DEEP_DIVE_SELECT, load_app_config())
    ok = ng = 0
    with OUT.open("a", encoding="utf-8") as f:
        for i, r in enumerate(todo, 1):
            v = await judge(llm, texts[r["article_id"]])
            if v is None:
                ng += 1
                print(f"{i:4d} FAIL {r['article_id'][:50]}", flush=True)
                continue
            ok += 1
            f.write(
                json.dumps(
                    {
                        "article_id": r["article_id"],
                        "stratum": r["stratum"],
                        "window_end": r["window_end"],
                        **v.model_dump(),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            f.flush()
            if i % 20 == 0:
                print(f"  {i}/{len(todo)}", flush=True)
    print(f"\n完了: 判定 {ok} / 失敗 {ng} → {OUT}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", default="claudecode:sonnet")
    p.add_argument(
        "--ids-file",
        help=(
            "審判する article_id を 1 行 1 件で書いたファイル (層=targeted)。"
            "⚠ 腕の比較には**同じ窓に集中した**審判が要る — 110 件を 13 窓へ散らすと"
            "1 窓 8 件になり、両腕の上位 20 が同じ集合を指して比較にならない (実測)"
        ),
    )
    p.add_argument("--apply", action="store_true", help="外部 LLM を呼ぶ (既定は dry-run)")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
