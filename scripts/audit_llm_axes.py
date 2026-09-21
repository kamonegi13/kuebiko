#!/usr/bin/env python3
"""LLM が付ける「軸」が実際に判断しているかを測る (2026-09-21)。

⭐ **働いていない軸は、外形からは見えない**。深掘りの timeliness は 74% が満点 5・
標準偏差 0.60 で、composite の重み 0.20 を持ちながら選抜に何も寄与していなかった。
さらに novelty の最下位 (「過去 4 週に選定済」) は**プールの段階で除外済み**のため
到達不能で、rubric に書いてあるだけだった。どちらも出力は正常に見える。

測るのは 3 つ:

- **偏り** — 最頻値が占める割合。高いほど「いつも同じ答え」
- **散らばり** — 標準偏差 / エントロピー。低いほど判断していない
- **実際に出ている値** — 上位 3 つを % つきで出す

⚠⚠ **期待値を自分で書かない**。初版は「high/medium/low のはず」と書いた列に対し
`medium が一度も出ない` と報告したが、実際の語彙は `high/moderate/low` で健全に
散っていた — **自分の思い込みを検査していた**。語彙はコードと DB が持っている。

⚠⚠ **偏りだけで「死んでいる」と言わない**。初版は change_kind が 99% `add` なのを
死んだ軸と報告したが、語彙は `add|correct` で訂正報道は現実に稀 (658 対 5)。
上の注意書きを自分で書いておきながら報告で破った。**判定はせず、数字を出して人が読む。**

    docker exec kuebiko python scripts/audit_llm_axes.py
"""

from __future__ import annotations

import argparse
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.storage.run_history import RunHistoryRepository  # noqa: E402

#: (表示名, テーブル, 列)
_AXES: tuple[tuple[str, str, str], ...] = (
    ("triage 重要度", "articles", "importance"),
    ("主題アクター確信度", "articles", "subject_actor_confidence"),
    ("intent 確信度", "articles", "intent_confidence"),
    ("socio-political intent", "articles", "socio_political_intent"),
    ("事象の重要度", "event_items", "importance"),
    ("事象の変化種別", "event_items", "change_kind"),
    ("状況の確信度", "situation_revisions", "confidence"),
    ("深掘り pir", "f1_selections", "pir"),
    ("深掘り roi", "f1_selections", "roi"),
    ("深掘り timeliness", "f1_selections", "timeliness"),
    ("深掘り novelty", "f1_selections", "novelty"),
)


def entropy(counts: Counter[Any]) -> float:
    """選択肢分布のエントロピー (bit)。0 = いつも同じ答え。"""
    n = sum(counts.values())
    if n == 0:
        return 0.0
    return -sum((c / n) * math.log2(c / n) for c in counts.values() if c)


def stdev(counts: Counter[Any]) -> float | None:
    """数値の軸なら標準偏差 (母標準偏差)。文字列の軸は None。"""
    vals: list[float] = []
    for v, c in counts.items():
        try:
            vals.extend([float(v)] * c)
        except (TypeError, ValueError):
            return None
    if not vals:
        return None
    m = sum(vals) / len(vals)
    return math.sqrt(sum((x - m) ** 2 for x in vals) / len(vals))


def fetch_counts(repo: RunHistoryRepository, table: str, column: str) -> Counter[Any]:
    """その列の値の出現数。

    期間では絞らない — 見たいのは推移ではなく「そもそも散っているか」なので、
    全期間のほうが標本が大きく判断しやすい。
    """
    sql = (  # noqa: S608 — table/column は本ファイルの定数のみ
        f"SELECT {column} AS v, count(*) AS n FROM {table} "
        f"WHERE {column} IS NOT NULL GROUP BY {column}"
    )
    with repo._connect() as conn:  # noqa: SLF001 — 読み取り専用の接続 seam 共有
        return Counter({r["v"]: int(r["n"]) for r in conn.execute(sql).fetchall()})


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    repo = RunHistoryRepository()

    print(f"{'軸':24s} {'件数':>7s} {'値数':>4s} {'最頻占有':>6s} {'散らばり':>8s}  実際の値")
    print("-" * 86)
    for label, table, column in _AXES:
        try:
            counts = fetch_counts(repo, table, column)
        except Exception as exc:  # noqa: BLE001 — 1 軸の失敗で監査を止めない
            print(f"{label:24s} 取得できず ({type(exc).__name__})")
            continue
        n = sum(counts.values())
        if not n:
            print(f"{label:24s} {'0':>7s}  (データなし)")
            continue
        top, top_n = counts.most_common(1)[0]
        sd = stdev(counts)
        spread = f"σ{sd:.2f}" if sd is not None else f"H{entropy(counts):.2f}"
        _ = top
        top3 = " ".join(f"{str(v)[:9]}={c / n * 100:.0f}%" for v, c in counts.most_common(3))
        print(f"{label:24s} {n:7d} {len(counts):4d} {top_n / n * 100:5.0f}% {spread:>8s}  {top3}")
    print(
        "\n⚠ この表は判定しない。偏りは欠陥とは限らない (訂正報道が稀なのは正常)。"
        "\n⚠ 選抜後のテーブル (f1_selections 等) の分布は**選ばれた後**のもの。"
        "軸が働いているかは**選抜前の母集団**で見ること。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
