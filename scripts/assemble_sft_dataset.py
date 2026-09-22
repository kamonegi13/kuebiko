#!/usr/bin/env python3
"""多タスク SFT の学習データを組み立てる (S 族 / N 族の両系譜に対応)。

単一タスクで学習した v1 は、**学習時に見ていないスキーマ**で縮退した (同一 ID の重複
56.8% / 空欄に文字列 "null")。よって複数課題を混ぜ、どの課題かはプロンプトに教えさせる。

不変条件 (どれも v1 の失敗から来ている):
- **切り詰めない。上限を超える標本は除外する**。mlx-lm の「上限ちょうどへの切り詰め」が
  学習中の NaN を起こした実績がある (2026-09-03)。
- **文字数をトークン数の代理にしない**。実トークン数をチャットテンプレート込みで数える。
- **「正しく空にする」例が各課題に含まれていることを確認してから出す**。

2026-09-07 拡張 (S1.5/N1.5):
- 出力は ``{"messages": [...]}`` 形式に統一。**CompletionsDataset と ChatDataset は
  同一トークン化**であることを実装読みで確認済み (system 無し行は従来と等価)。
  S 第 2 陣 (pair_judge/event_kind/pir_judge) は本番が system prompt を使うため、
  学習側も system turn を持たないと serving と書式が食い違う (gemma は system を
  独立 turn として持つ — user への折り込みではない)。
- 課題の判別はプロンプト推測をやめ**読み込み元でタグ付け** (誤判別の余地を消す)。
- ⚠ spotlight は**識別子のオフライン関門を通した後のファイルを渡す**こと
  (scripts/filter_spotlight_teacher.py — 本スクリプトは mlx venv で動くため src の
  依存連鎖 (structlog 等) を辿れず、関門を内蔵すると黙って壊れる)。

使用例:
    # S1.5 (構造化族): 事象/spotlight を除外
    data/mlx/venv/bin/python scripts/assemble_sft_dataset.py \\
        --eventnews /nonexistent --spotlight /nonexistent \\
        --cap-triage 750 --out-dir data/mlx/dataset_s15
    # N1.5 (narrative 族): 構造化系を除外、長系列を許容
    data/mlx/venv/bin/python scripts/assemble_sft_dataset.py \\
        --summary /nonexistent --triage /nonexistent --pair-judge /nonexistent \\
        --event-kind /nonexistent --pir-judge /nonexistent \\
        --max-tokens 17500 --out-dir data/mlx/dataset_n15
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 事象ニュースの旧ファイル (data/mlx/dataset) は prompt/completion のみでタグが無い。
_TEACHER = Path("data/mlx/teacher")


#: 課題名 → 接頭辞。**本番と同じ SSoT** (`src/tools/task_prefix.TASK_MARKERS`) から
#: 写した値。⚠ mlx venv では src の依存連鎖を辿れないため、ここは写しになる —
#: 値がずれると「学習した形」と「本番で届く形」が食い違うので、両方を必ず一緒に直す
#: (2026-09-22、多課題 SFT の負の転移対策)。
TASK_PREFIXES: dict[str, str] = {
    "triage": "[task: triage]\n",
    "article_summary": "[task: summary]\n",
    "pair_judge": "[task: pair]\n",
    "event_kind": "[task: kind]\n",
    "pir_judge": "[task: pir]\n",
    "detect": "[task: detect]\n",
    "ach": "[task: ach]\n",
    "eventnews": "[task: event_news]\n",
    "spotlight": "[task: spotlight]\n",
}


def _load_pairs(path: Path, task: str, *, with_prefix: bool = True) -> list[dict[str, str]]:
    """1 ファイル = 1 課題としてタグ付きで読む。skip マーカー行は捨てる。"""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        if d.get("skipped"):
            continue
        if d.get("prompt") and d.get("completion"):
            # ⭐ 課題の接頭辞は **読み込み時に付ける** — 以後のトークン計測も接頭辞込みに
            #   なり、学習時の系列長と一致する (2026-09-22)。
            # ⭐ 置くのは **系列の先頭** (system があれば system の先頭)。system の後ろに
            #   置くと、課題を定義する長い文の後に印が来て切替の合図として働きにくい。
            marker = TASK_PREFIXES.get(task, "") if with_prefix else ""
            prompt, system = d["prompt"], d.get("system") or ""
            if marker:
                if system and not system.startswith(marker):
                    system = marker + system
                elif not system and not prompt.startswith(marker):
                    prompt = marker + prompt
            row = {"prompt": prompt, "completion": d["completion"], "_task": task}
            if system:
                row["system"] = system
            out.append(row)
    return out


def _select_capped(pairs: list[dict[str, str]], cap: int) -> list[dict[str, str]]:
    """課題の上限まで絞る。**クラス比は保ち、feed の偏りだけ均す** (triage 用)。"""
    if len(pairs) <= cap:
        return pairs

    def cls(p: dict[str, str]) -> str:
        try:
            return str(json.loads(p["completion"]).get("importance", "?"))
        except json.JSONDecodeError:
            return "?"

    def feed(p: dict[str, str]) -> str:
        m = re.search(r"フィード:\s*(.+)", p["prompt"])
        return m.group(1).strip() if m else "?"

    by_class: dict[str, dict[str, list[dict[str, str]]]] = {}
    for p in pairs:
        by_class.setdefault(cls(p), {}).setdefault(feed(p), []).append(p)

    out: list[dict[str, str]] = []
    for _cls_name, feeds in by_class.items():
        quota = round(cap * sum(len(v) for v in feeds.values()) / len(pairs))
        picked: list[dict[str, str]] = []
        queues = [list(v) for v in feeds.values()]
        while len(picked) < quota and any(queues):  # feed をラウンドロビン
            for q in queues:
                if q and len(picked) < quota:
                    picked.append(q.pop(0))
            queues = [q for q in queues if q]
        out.extend(picked)
    return out


def _empty_field_count(completion: str) -> int:
    """completion のうち「正しく空」になっている欄の数。"""
    try:
        payload = json.loads(completion)
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0
    return sum(1 for v in payload.values() if v is None or v == [] or v == "")


def _messages_of(pair: dict[str, str]) -> list[dict[str, str]]:
    msgs = []
    if pair.get("system"):
        msgs.append({"role": "system", "content": pair["system"]})
    msgs.append({"role": "user", "content": pair["prompt"]})
    msgs.append({"role": "assistant", "content": pair["completion"]})
    return msgs


def _token_len(tokenizer: Any, pair: dict[str, str]) -> int:
    """チャットテンプレート込みの実トークン数 (学習時と同じ数え方)。"""
    msgs = _messages_of(pair)
    try:
        rendered = tokenizer.apply_chat_template(
            msgs[:-1], tokenize=False, add_generation_prompt=True
        )
    except Exception:  # noqa: BLE001 — テンプレート未提供の tokenizer は素の連結で数える
        rendered = pair.get("system", "") + pair["prompt"]
    return len(tokenizer.encode(rendered)) + len(tokenizer.encode(pair["completion"]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--eventnews", type=Path, default=Path("data/mlx/dataset"))
    ap.add_argument("--summary", type=Path, default=_TEACHER / "article_summary.jsonl")
    ap.add_argument("--triage", type=Path, default=_TEACHER / "triage.jsonl")
    ap.add_argument("--pair-judge", type=Path, default=_TEACHER / "pair_judge.jsonl")
    ap.add_argument("--event-kind", type=Path, default=_TEACHER / "event_kind.jsonl")
    ap.add_argument("--pir-judge", type=Path, default=_TEACHER / "pir_judge.jsonl")
    ap.add_argument("--spotlight", type=Path, default=_TEACHER / "spotlight.jsonl")
    # detect (台帳の開設候補) — 2026-09-22 に教師を収穫 (as-of + 埋込の絞り込み入り)。
    # ⚠ 教師は cur の腕のみ (ml_add は ML の 4 件を 99 日中 51 日で全件承認していて
    #   判断を教えていない)。
    ap.add_argument("--detect", type=Path, default=_TEACHER / "detect.jsonl")
    ap.add_argument("--ach", type=Path, default=_TEACHER / "ach_opus.jsonl")
    ap.add_argument("--out-dir", type=Path, default=Path("data/mlx/dataset_v2"))
    ap.add_argument("--model", default="mlx-community/gemma-4-26b-a4b-it-8bit")
    ap.add_argument("--valid-size", type=int, default=60)
    ap.add_argument("--seed", type=int, default=13)
    # ⚠ **学習の --max-seq-length より必ず小さくする** (2026-09-22)。12,500 の学習に対し
    #   13,000 で組み立てたところ、6 件が上限超過で切り詰められ、うち 1 件
    #   (全 12,940 tok / prompt 12,110 tok) で **Train loss が NaN** になった。
    #   切り詰めは学習対象を 830 → 390 tok に削る。学習率にも最適化器にも依存しない
    #   (数値の発散ではなく切り詰めの問題)。組み立て側で除外するのが正しい。
    ap.add_argument("--max-tokens", type=int, default=12000, help="超過標本は除外 (切り詰めない)")
    ap.add_argument("--cap-triage", type=int, default=0, help="triage の上限 (0 で無制限)")
    ap.add_argument("--cap-summary", type=int, default=0, help="article_summary の上限")
    ap.add_argument("--cap-eventnews", type=int, default=0, help="事象ニュースの上限")
    # ⚠ 接頭辞は **本番の SFT_TASK_PREFIX と必ず揃える**。片方だけだと生徒が見たことの
    #   ない形になる (2026-09-22、多課題 SFT の負の転移対策)。
    ap.add_argument("--no-task-prefix", action="store_true", help="課題の接頭辞を付けない")
    args = ap.parse_args()

    pairs: list[dict[str, str]] = []
    pairs += _load_pairs(
        args.eventnews / "train.jsonl", "eventnews", with_prefix=not args.no_task_prefix
    )
    pairs += _load_pairs(
        args.eventnews / "valid.jsonl", "eventnews", with_prefix=not args.no_task_prefix
    )
    pairs += _load_pairs(args.summary, "article_summary", with_prefix=not args.no_task_prefix)
    pairs += _load_pairs(args.triage, "triage", with_prefix=not args.no_task_prefix)
    pairs += _load_pairs(args.pair_judge, "pair_judge", with_prefix=not args.no_task_prefix)
    pairs += _load_pairs(args.event_kind, "event_kind", with_prefix=not args.no_task_prefix)
    pairs += _load_pairs(args.pir_judge, "pir_judge", with_prefix=not args.no_task_prefix)
    pairs += _load_pairs(args.spotlight, "spotlight", with_prefix=not args.no_task_prefix)
    pairs += _load_pairs(args.detect, "detect", with_prefix=not args.no_task_prefix)
    pairs += _load_pairs(args.ach, "ach", with_prefix=not args.no_task_prefix)
    if not pairs:
        print("入力が空", file=sys.stderr)
        return 1

    from mlx_lm.utils import load  # 遅延 import (mlx は arm64 venv のみ)

    _, tokenizer = load(args.model)

    kept: list[dict[str, str]] = []
    dropped: Counter[str] = Counter()
    for pair in pairs:
        task = pair["_task"]
        n = _token_len(tokenizer, pair)
        if n > args.max_tokens:
            dropped[task] += 1
            continue
        kept.append(pair)

    # 課題ごとの上限。比率は結果を左右する一級のつまみ (v2 の干渉実測より)。
    caps = {
        "triage": args.cap_triage,
        "article_summary": args.cap_summary,
        "eventnews": args.cap_eventnews,
    }
    for task, cap in caps.items():
        if not cap:
            continue
        group = [p for p in kept if p["_task"] == task]
        if len(group) > cap:
            selected = _select_capped(group, cap)
            print(f"{task} を {len(group)} → {len(selected)} 件に選別")
            kept = [p for p in kept if p["_task"] != task] + selected

    by_task = Counter(p["_task"] for p in kept)
    print("=== 課題ごとの構成 ===")
    total = len(kept)
    for task, n in by_task.most_common():
        empties = sum(1 for p in kept if p["_task"] == task and _empty_field_count(p["completion"]))
        print(
            f"  {task:16s} {n:5d} 件 ({100 * n / total:4.1f}%) "
            f"/ 上限超過で除外 {dropped[task]:3d} / **空欄を含む例 {empties:4d} 件**"
        )

    # 「正しく空にする」例が無い課題があると v1 の "null" 欠陥が再発する。
    # 任意欄を持たない schema の課題は常に全欄が埋まるのが正しいため対象外:
    # triage {importance, reason} / pair_judge {same_event, reason} /
    # event_kind {kind} / pir_judge (verdict 系) / spotlight (headline/outlook 必須)。
    _no_optional = {"triage", "pair_judge", "event_kind", "pir_judge", "spotlight"}
    missing = [
        t
        for t in by_task
        if t not in _no_optional
        and not any(p["_task"] == t and _empty_field_count(p["completion"]) for p in kept)
    ]
    if missing:
        print(
            f"\n⚠ 空欄の例が 1 件も無い課題: {missing} — この状態で学習すると再発する",
            file=sys.stderr,
        )

    random.Random(args.seed).shuffle(kept)
    valid, train = kept[: args.valid_size], kept[args.valid_size :]
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in (("train", train), ("valid", valid)):
        path = args.out_dir / f"{name}.jsonl"
        path.write_text(
            "".join(
                json.dumps({"messages": _messages_of(r)}, ensure_ascii=False) + "\n" for r in rows
            ),
            encoding="utf-8",
        )
        print(f"\n{path}: {len(rows)} 件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
