#!/usr/bin/env python3
"""triage_boundary_ab_v1.jsonl を v4 / v2 gold に対して採点する (読み取り専用、host 実行)。"""

from __future__ import annotations

import json
from pathlib import Path

EVAL = Path(__file__).resolve().parent.parent / "data" / "eval"
AB = EVAL / "triage_boundary_ab_v1.jsonl"
GOLD_V4 = EVAL / "triage_gold_v4.jsonl"
GOLD_V2 = EVAL / "triage_gold_v2.jsonl"
ERRORS_V4 = EVAL / "i58_triage_v4_errors.jsonl"

KNOWN_MODEL_ERROR_IDS = {
    "grok:8eb94dd3996c04ec:508#2095191258449006695",
    "rss:https://sputnikglobe.com/20260902/over-64-of-poles-oppose-transferring-patriot-missiles-to-ukraine--poll-1124661093.html",
    "grok:f2b66d0aea030948:512#2095236796535963925",
    "rss:https://therecord.media/hackers-russia-fundraisers-ukraine",
}


def _norm(label: str) -> str:
    return (label or "").strip().lower()


def load_gold(path: Path) -> dict[str, dict]:
    out = {}
    for line in path.open(encoding="utf-8"):
        r = json.loads(line)
        out[r["key"]] = r
    return out


def load_ab(path: Path) -> dict[tuple[str, str], dict]:
    out = {}
    for line in path.open(encoding="utf-8"):
        r = json.loads(line)
        out[(r["article_id"], r["arm"])] = r
    return out


def accuracy(keys: list[str], gold: dict, ab: dict, arm: str) -> tuple[int, int]:
    correct = 0
    total = 0
    for k in keys:
        pred_row = ab.get((k, arm))
        if pred_row is None:
            continue
        total += 1
        if _norm(pred_row["importance"]) == _norm(gold[k]["importance"]):
            correct += 1
    return correct, total


def heavy_errors(keys: list[str], gold: dict, ab: dict, arm: str) -> list[str]:
    out = []
    for k in keys:
        pred_row = ab.get((k, arm))
        if pred_row is None:
            continue
        g = _norm(gold[k]["importance"])
        p = _norm(pred_row["importance"])
        if g in ("high", "medium") and p == "low":
            out.append(k)
    return out


def main() -> None:
    gold_v4 = load_gold(GOLD_V4)
    gold_v2 = load_gold(GOLD_V2)
    ab = load_ab(AB)

    print(f"AB rows: {len(ab)} (expect {len(gold_v4)} * 2 = {len(gold_v4) * 2})")
    missing = [k for k in gold_v4 if (k, "A") not in ab or (k, "B") not in ab]
    if missing:
        print(f"WARNING: {len(missing)} 記事が未完了 (両 arm 揃っていない)")

    for label, gold in (("v4", gold_v4), ("v2", gold_v2)):
        all_keys = list(gold.keys())
        nonboundary_keys = [k for k in all_keys if not gold[k].get("boundary")]
        for scope_name, keys in (("all(150)", all_keys), ("non-boundary", nonboundary_keys)):
            print(f"\n=== {label} / {scope_name} (n={len(keys)}) ===")
            for arm in ("A", "B"):
                correct, total = accuracy(keys, gold, ab, arm)
                heavy = heavy_errors(keys, gold, ab, arm)
                acc_pct = 100.0 * correct / total if total else float("nan")
                print(
                    f"  arm {arm}: accuracy {correct}/{total} = {acc_pct:.1f}%  "
                    f"heavy_errors(high/medium→low)={len(heavy)} {heavy if heavy else ''}"
                )

    # A→B の label 変化 (v4 基準、150 件全件)
    print("\n=== A→B でラベルが変わった記事 (v4 gold 基準) ===")
    changes = []
    for k in gold_v4:
        a = ab.get((k, "A"))
        b = ab.get((k, "B"))
        if a is None or b is None:
            continue
        pa, pb = _norm(a["importance"]), _norm(b["importance"])
        if pa == pb:
            continue
        g = _norm(gold_v4[k]["importance"])
        was_correct = pa == g
        now_correct = pb == g
        if now_correct and not was_correct:
            verdict = "IMPROVEMENT"
        elif was_correct and not now_correct:
            verdict = "REGRESSION"
        else:
            verdict = "neutral (both wrong, different wrong)" if not was_correct else "neutral?"
        changes.append(
            {
                "article_id": k,
                "title": gold_v4[k].get("title", ""),
                "gold": g,
                "A": pa,
                "B": pb,
                "boundary": bool(gold_v4[k].get("boundary")),
                "verdict": verdict,
            }
        )
    for c in changes:
        print(
            f"  [{c['verdict']}] {c['article_id'][:60]!r} gold={c['gold']} A={c['A']}→B={c['B']} "
            f"boundary={c['boundary']} title={c['title'][:60]}"
        )
    n_imp = sum(1 for c in changes if c["verdict"] == "IMPROVEMENT")
    n_reg = sum(1 for c in changes if c["verdict"] == "REGRESSION")
    print(f"\n  total changed: {len(changes)}  improvements={n_imp}  regressions={n_reg}")

    print("\n=== 既知の 4 model_error 行の A/B ===")
    for k in KNOWN_MODEL_ERROR_IDS:
        a = ab.get((k, "A"))
        b = ab.get((k, "B"))
        g = gold_v4.get(k, {}).get("importance")
        if a is None or b is None:
            print(f"  {k[:60]!r}: AB データなし")
            continue
        print(
            f"  {k[:60]!r}: gold={g} A={_norm(a['importance'])} B={_norm(b['importance'])} "
            f"(v4誤りは A相当: low/medium/high列を確認)"
        )


if __name__ == "__main__":
    main()
