#!/usr/bin/env python3
"""教師×学生の乖離から IPO/DPO の preference ペアを組み立てる。

選定基準 (2026-09-06 に実データで測って決定 — 憶測の閾値を置かない):
- triage: 重要度ラベル不一致 (実測 18.1%、大半が隣接クラス = 難例で明白マージンでない)
- article_summary: importance/category 不一致 or IOC が教師の半分未満 (教師 2 件以上時)
  or 機械関門落ち (placeholder / list 内重複)
- event_news: 教師が 2 項目以上入れた欄 (caveats/discrepancies/unknowns) が学生 0
  or 学生出力に重複項目

出力: {"prompt", "chosen"(教師), "rejected"(学生)} jsonl (mlx-lm-lora DPODataset 形式)。
token 長は tokenizer で実測し ``--max-tokens`` 超の対を除外する (DPO は chosen+rejected の
2 系列を持つため SFT より peak memory が嵩む — IPO smoke の実測に基づく上限)。

⚠ これはレシピ確定用 (S1/N1 由来)。本走 N2/S2 は N1.5/S1.5 の学生出力で組み直す。
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_PLACE = {"null", "none", "n/a", "na", "不明", "なし", "-"}
_VALID_RATIO = 0.05  # valid 分割 (時間でなく無作為 — ペアは既に train 期間の材料)


def _load(path: Path, key: str) -> dict[str, dict[str, Any]]:
    out = {}
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        d = json.loads(line)
        if d.get("skipped"):
            continue
        out[str(d.get(key, i))] = d
    return out


def _gate_fail(payload: dict[str, Any]) -> bool:
    for v in payload.values():
        if not isinstance(v, list):
            continue
        ss = [
            json.dumps(x, ensure_ascii=False, sort_keys=True) if not isinstance(x, str) else x
            for x in v
        ]
        if len(ss) != len(set(ss)):
            return True
        if any(isinstance(x, str) and x.strip().casefold() in _PLACE for x in ss):
            return True
    return False


def _pairs_triage(teacher: Path, student: Path) -> list[dict[str, str]]:
    t, s = _load(teacher, "article_id"), _load(student, "article_id")
    out = []
    for k in set(t) & set(s):
        te, st = json.loads(t[k]["completion"]), json.loads(s[k]["student"])
        if te["importance"] != st["importance"]:
            out.append(
                {
                    "prompt": t[k]["prompt"],
                    "chosen": t[k]["completion"],
                    "rejected": s[k]["student"],
                }
            )
    return out


def _pairs_summary(teacher: Path, student: Path) -> list[dict[str, str]]:
    t, s = _load(teacher, "article_id"), _load(student, "article_id")
    out = []
    for k in set(t) & set(s):
        te, st = json.loads(t[k]["completion"]), json.loads(s[k]["student"])
        ti, si = te.get("iocs") or [], st.get("iocs") or []
        diverged = (
            te.get("importance") != st.get("importance")
            or te.get("category") != st.get("category")
            or (len(ti) >= 2 and len(si) < len(ti) / 2)
            or _gate_fail(st)
        )
        if diverged:
            out.append(
                {
                    "prompt": t[k]["prompt"],
                    "chosen": t[k]["completion"],
                    "rejected": s[k]["student"],
                }
            )
    return out


def _pairs_event(teacher: Path, student: Path) -> list[dict[str, str]]:
    t = {}
    for i, line in enumerate(teacher.read_text(encoding="utf-8").splitlines()):
        if line.strip():
            t[f"{teacher.stem}:{i}"] = json.loads(line)
    s = _load(student, "article_id")
    out = []
    for k in set(t) & set(s):
        te, st = json.loads(t[k]["completion"]), json.loads(s[k]["student"])
        under = any(
            len(te.get(f) or []) >= 2 and len(st.get(f) or []) == 0
            for f in ("caveats", "discrepancies", "unknowns")
        )
        if under or _gate_fail(st):
            out.append(
                {
                    "prompt": t[k]["prompt"],
                    "chosen": t[k]["completion"],
                    "rejected": s[k]["student"],
                }
            )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", type=Path, default=Path("data/mlx/dpo/pairs_recipe"))
    ap.add_argument(
        "--max-tokens", type=int, default=8000, help="prompt+長い方の completion の上限"
    )
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument(
        "--only",
        choices=["s", "n", "all"],
        default="all",
        help="族の選択 (IPO は族ごとに別モデルへ掛けるため混ぜない: s=triage+summary / n=event)",
    )
    args = ap.parse_args()

    pairs: list[dict[str, str]] = []
    if args.only in ("s", "all"):
        pairs += _pairs_triage(
            Path("data/mlx/teacher/triage.jsonl"), Path("data/mlx/dpo/student_s1_triage.jsonl")
        )
        pairs += _pairs_summary(
            Path("data/mlx/teacher/article_summary.jsonl"),
            Path("data/mlx/dpo/student_s1_summary.jsonl"),
        )
    if args.only in ("n", "all"):
        pairs += _pairs_event(
            Path("data/mlx/dpo/event_teacher_all.jsonl"),
            Path("data/mlx/dpo/student_n1_event.jsonl"),
        )
    print(f"乖離ペア {len(pairs)} 対", file=sys.stderr)

    # token 長の実測 (SFT 組み立てと同じ tokenizer 経由) と上限適用
    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained("mlx-community/gemma-4-26b-a4b-it-4bit")
    kept, dropped = [], 0
    lengths = []
    for p in pairs:
        n = len(tok.encode(p["prompt"])) + max(
            len(tok.encode(p["chosen"])), len(tok.encode(p["rejected"]))
        )
        lengths.append(n)
        if n <= args.max_tokens:
            kept.append(p)
        else:
            dropped += 1
    lengths.sort()
    if lengths:
        print(
            f"token 長: 中央 {lengths[len(lengths) // 2]} / p90 {lengths[int(len(lengths) * 0.9)]}"
            f" / 最大 {lengths[-1]} / 上限超で除外 {dropped}",
            file=sys.stderr,
        )

    random.Random(args.seed).shuffle(kept)
    n_valid = max(8, int(len(kept) * _VALID_RATIO))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    with (args.out_dir / "valid.jsonl").open("w", encoding="utf-8") as f:
        for p in kept[:n_valid]:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    with (args.out_dir / "train.jsonl").open("w", encoding="utf-8") as f:
        for p in kept[n_valid:]:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    print(f"train {len(kept) - n_valid} / valid {n_valid} → {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
