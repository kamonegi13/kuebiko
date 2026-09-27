#!/usr/bin/env python3
"""学習データ v2a を組み立てる (2026-09-27、docs/research/llm_training/training_recipe_v2.md §2)。

s20 のデータ (data/mlx/dataset_s20) に次をまとめて適用する (1 本の学習で試す — 利用者判断):
- 要約: 教師出力から本番で出させない欄を消す (bluf / pmesii_axes / diamond / event_date 等)
- 全課題: 入力の本文がナビの残骸の例を除く (教師が見出しや自身の知識から推測を書いていた)
- detect: プロンプトの期間表記「YYYY-MM-DD (teacher)」を本番の形 (JST の範囲) に直す
- pair: 同一/別の比率を保って 300 例に減らす (SFT で回答が変わらない課題に勾配の 3 割を使っていた)
- MITRE ATT&CK の知識 QA を 200 例 (技術 120 / グループ 50 / 緩和策 30) 足す。CTIBench (評価) に
  ID・名前・別名が出る項目と、答えが 8 語以上一致する項目は除く (汚染の防止)

    uv run python scripts/cloud_train/build_dataset_v2.py \\
        --src data/mlx/dataset_s20 --knowledge data/mlx/knowledge --out data/mlx/dataset_v2a
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
from collections import Counter
from pathlib import Path
from typing import Any

SUPPRESSED = (
    "bluf",
    "pmesii_axes",
    "diamond",
    "event_date",
    "event_date_basis",
    "compromise_date",
    "article_type",
    "editorial_stance",
    "routing_flags",
)
NAV = re.compile(
    r"MOST POPULAR|Subscribe to|Accept (all )?cookies|Sign up for our newsletter"
    r"|All rights reserved",
    re.I,
)
TEACHER_PERIOD = re.compile(r"(\d{4}-\d{2}-\d{2}) \(teacher\)")
PAIR_KEEP = 300
KNOWLEDGE = {"course-of-action": 30, "intrusion-set": 50, "attack-pattern": 120}
#: 知識 QA の合計。除染で足りない種類の分は、最後の種類 (技術) で補う
KNOWLEDGE_TOTAL = 200
NGRAM = 8
ANSWER_MAX = 700
SEED = 927


def task(r: dict[str, Any]) -> str:
    u = " ".join(m["content"] for m in r["messages"] if m["role"] != "assistant")
    m = re.search(r"\[task: (\w+)\]", u)
    return m.group(1) if m else "none"


def fix_example(r: dict[str, Any], stats: Counter[str]) -> dict[str, Any] | None:
    """1 例を直した新しい例 (除外なら None)。元は変更しない。"""
    t = task(r)
    user = "\n".join(m["content"] for m in r["messages"] if m["role"] != "assistant")
    if NAV.search(user):
        stats[f"除外 ナビの残骸 {t}"] += 1
        return None
    msgs = [dict(m) for m in r["messages"]]
    if t == "detect":
        for m in msgs:
            if m["role"] != "assistant" and TEACHER_PERIOD.search(m["content"]):
                m["content"] = TEACHER_PERIOD.sub(r"\1 00:00 〜 \1 23:59 JST", m["content"])
                stats["detect の期間表記を直した"] += 1
    if t == "summary":
        target = json.loads(msgs[-1]["content"])
        dropped = [k for k in SUPPRESSED if k in target]
        target = {k: v for k, v in target.items() if k not in SUPPRESSED}
        msgs[-1]["content"] = json.dumps(target, ensure_ascii=False)
        stats["要約の出させない欄を消した"] += bool(dropped)
    return {"messages": msgs}


def _clean(text: str) -> str:
    text = re.sub(r"\(Citation:[^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"<code>|</code>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > ANSWER_MAX:
        cut = text[:ANSWER_MAX]
        text = cut[: cut.rfind(". ") + 1] or cut
    return text


def _ngrams(text: str) -> set[tuple[str, ...]]:
    w = re.findall(r"[a-z0-9]+", text.lower())
    return {tuple(w[i : i + NGRAM]) for i in range(len(w) - NGRAM + 1)}


def _bench(knowledge: Path) -> tuple[str, set[tuple[str, ...]]]:
    """CTIBench の全文 (小文字) と 8-gram 集合。"""
    parts: list[str] = []
    for name in ("cti-mcq.tsv", "cti-ate.tsv", "cti-taa.tsv"):
        with (knowledge / name).open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                parts.append(" ".join(v for v in row.values() if v))
    text = "\n".join(parts).lower()
    return text, _ngrams(text)


def knowledge_examples(knowledge: Path, stats: Counter[str]) -> list[dict[str, Any]]:
    bench_text, bench_grams = _bench(knowledge)
    objs = json.loads((knowledge / "enterprise-attack.json").read_text(encoding="utf-8"))["objects"]
    rng = random.Random(SEED)
    out: list[dict[str, Any]] = []
    for kind, want in KNOWLEDGE.items():
        if kind == list(KNOWLEDGE)[-1]:
            want = KNOWLEDGE_TOTAL - len(out)
        pool = []
        for o in objs:
            if o.get("type") != kind or o.get("revoked") or o.get("x_mitre_deprecated"):
                continue
            ext = next(
                (
                    r
                    for r in o.get("external_references", [])
                    if r.get("source_name") == "mitre-attack"
                ),
                {},
            )
            ext_id, name = ext.get("external_id", ""), o.get("name", "")
            desc = _clean(o.get("description", ""))
            if not ext_id or len(desc) < 80:
                continue
            names = [name, *o.get("aliases", [])[1:]] if kind == "intrusion-set" else [name]
            if ext_id.lower() in bench_text or any(
                len(n) > 3 and re.search(rf"\b{re.escape(n.lower())}\b", bench_text) for n in names
            ):
                stats[f"除外 CTIBench に ID/名前 {kind}"] += 1
                continue
            if _ngrams(desc) & bench_grams:
                stats[f"除外 CTIBench と文章が一致 {kind}"] += 1
                continue
            pool.append((ext_id, name, desc, names))
        for ext_id, name, desc, names in rng.sample(pool, min(want, len(pool))):
            if kind == "attack-pattern":
                q = f"Explain the MITRE ATT&CK technique {ext_id} ({name})."
            elif kind == "intrusion-set":
                aka = f" (also known as {', '.join(names[1:4])})" if len(names) > 1 else ""
                q = f"Describe the threat group {name}{aka} tracked by MITRE ATT&CK as {ext_id}."
            else:
                q = f"Describe the MITRE ATT&CK mitigation {ext_id} ({name})."
            out.append(
                {
                    "messages": [
                        {"role": "user", "content": q},
                        {"role": "assistant", "content": desc},
                    ],
                    "source": f"mitre-attack:{ext_id}",
                }
            )
        stats[f"知識 QA {kind}"] = min(want, len(pool))
    return out


def build(src: Path, knowledge: Path, out: Path) -> Counter[str]:
    stats: Counter[str] = Counter()
    rng = random.Random(SEED)
    out.mkdir(parents=True, exist_ok=True)
    for split in ("train", "valid"):
        rows = [json.loads(x) for x in (src / f"{split}.jsonl").open() if x.strip()]
        fixed = [e for e in (fix_example(r, stats) for r in rows) if e is not None]
        if split == "train":
            pairs = [e for e in fixed if task(e) == "pair"]
            others = [e for e in fixed if task(e) != "pair"]
            keep = rng.sample(pairs, min(PAIR_KEEP, len(pairs)))
            stats["pair を減らした"] = len(pairs) - len(keep)
            fixed = others + keep + knowledge_examples(knowledge, stats)
            rng.shuffle(fixed)
        with (out / f"{split}.jsonl").open("w", encoding="utf-8") as fh:
            for e in fixed:
                fh.write(json.dumps({"messages": e["messages"]}, ensure_ascii=False) + "\n")
        stats[f"{split} 件数"] = len(fixed)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", type=Path, default=Path("data/mlx/dataset_s20"))
    ap.add_argument("--knowledge", type=Path, default=Path("data/mlx/knowledge"))
    ap.add_argument("--out", type=Path, default=Path("data/mlx/dataset_v2a"))
    args = ap.parse_args()
    for k, v in sorted(build(args.src, args.knowledge, args.out).items()):
        print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
