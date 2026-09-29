#!/usr/bin/env python3
"""学習データ v2a を組み立てる (2026-09-27、docs/research/llm_training/training_recipe_v2.md §2)。

s20 のデータ (data/mlx/dataset_s20) に次をまとめて適用する (1 本の学習で試す — 利用者判断):
- 要約: 教師出力から本番で出させない欄を消す (bluf / pmesii_axes / diamond / event_date 等)
- 全課題: 入力の本文がナビの残骸の例を除く (教師が見出しや自身の知識から推測を書いていた)
- detect: プロンプトの期間表記「YYYY-MM-DD (teacher)」を本番の形 (JST の範囲) に直す
- pair: 同一/別の比率を保って 300 例に減らす (SFT で回答が変わらない課題に勾配の 3 割を使っていた)
- (v2c) 要約の ATT&CK 技術を、本文と照らした判定 (data/mlx/judge_teacher_ttp.py、Opus・原文引用つき)
  で掃除する: 「明記」「文面から明らか」だけ残し「推測」を落とす (教師は 1 記事 5.3 個・本文で
  裏付けられるのは約 6 割。生徒は当て推量の技術まで写し、s20 は 8.2 個・裏付け 5% に増幅した)
- (s22) triage の教師を判定基準に照らして付け直す (data/mlx/build_triage_relabel.py、Opus 判定 +
  利用者決定: サイバーの要素がない軍事・宇宙は high にしない)。
  教師の 2 割が基準とずれ、high の 4 割が過大だった
- MITRE ATT&CK の知識 QA を 200 例 (技術 120 / グループ 50 / 緩和策 30) 足す。CTIBench (評価) に
  ID・名前・別名が出る項目と、答えが 8 語以上一致する項目は除く (汚染の防止)

    uv run python scripts/cloud_train/build_dataset_v2.py \\
        --src data/mlx/dataset_s20 --knowledge data/mlx/knowledge --out data/mlx/dataset_v2a
"""

from __future__ import annotations

import argparse
import csv
import hashlib
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


#: 技術の判定で残すもの。引用が本文に無かったもの ("推測(引用不一致)") と「推測」は落とす
KEEP_VERDICTS = ("明記", "文面から明らか")


def load_ttp_verdicts(path: Path | None) -> dict[str, dict[str, dict[str, Any]]]:
    """例の鍵 → {技術 ID: 判定}。鍵は judge_teacher_ttp.py と同じ (教師出力より前の sha256)。"""
    if path is None or not path.exists():
        return {}
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for line in path.open(encoding="utf-8"):
        row = json.loads(line)
        out[row["key"]] = {t["technique"].upper(): t for t in row["techniques"]}
    return out


def _example_key(r: dict[str, Any]) -> str:
    user = "\n".join(m["content"] for m in r["messages"][:-1])
    return hashlib.sha256(user.encode()).hexdigest()[:16]


def clean_techniques(
    techs: list[Any], verdicts: dict[str, dict[str, Any]], stats: Counter[str]
) -> list[str]:
    """判定に基づいて技術を残す。判定の無い技術は、決定論の関門を通ったものだけ残す。"""
    kept = []
    for t in techs:
        if not isinstance(t, str):
            continue
        v = verdicts.get(t.upper())
        if v is None or v["final"] == "未判定":
            keep = bool(v and v.get("gate"))
            stats["技術 判定なし→" + ("関門通過で残す" if keep else "落とす")] += 1
        else:
            keep = v["final"] in KEEP_VERDICTS
            stats[f"技術 {v['final']}"] += 1
        if keep:
            kept.append(t)
    return kept


def evidence_for(
    techs: list[str], verdicts: dict[str, dict[str, Any]], stats: Counter[str]
) -> list[dict[str, str]]:
    """残した技術ごとの原文の引用 (s22 の ``mitre_evidence`` 欄、2026-09-29)。

    引用が本文と照合できなかった技術は、欄に入れず技術からも落とす側で扱う
    (呼び出し側が ``techs`` を引用ありに絞る)。
    """
    out = []
    for t in techs:
        v = verdicts.get(t.upper())
        if v and v.get("quote_ok") and v.get("quote"):
            out.append({"technique": t, "quote": v["quote"]})
        else:
            stats["引用なし"] += 1
    return out


def load_summary_corrected(path: Path | None) -> dict[int, str]:
    """train の行番号 → 書き直した要約 (correct_summary_teacher.py の出力)。"""
    if path is None or not path.exists():
        return {}
    return {
        int(row["i"]): str(row["completion"])
        for row in (json.loads(x) for x in path.open(encoding="utf-8") if x.strip())
    }


#: PIR 別の要点の接頭辞 (src/tools/task_prefix.py の TASK_MARKERS と同じ)
PIR_FOCUS_MARKER = "[task: pir_focus]\n"
#: 検証に回す割合 (学習と重ならない)
PIR_FOCUS_VALID_EVERY = 20


def pir_focus_examples(path: Path | None, stats: Counter[str]) -> tuple[list[Any], list[Any]]:
    """PIR 別の要点の教師対 (build_sft_teacher_pir_focus.py) → (train, valid) の例。

    本番は system なしで prompt を渡すので、接頭辞は user の先頭に付ける。
    """
    if path is None or not path.exists():
        return [], []
    train: list[Any] = []
    valid: list[Any] = []
    rows = [json.loads(x) for x in path.open(encoding="utf-8") if x.strip()]
    for i, row in enumerate(sorted(rows, key=lambda r: r["key"])):
        text = str(row["completion"]).strip()
        if not text:
            continue
        ex = {
            "messages": [
                {"role": "user", "content": PIR_FOCUS_MARKER + row["prompt"]},
                {"role": "assistant", "content": text},
            ]
        }
        (valid if i % PIR_FOCUS_VALID_EVERY == 0 else train).append(ex)
    stats["PIR 別の要点 train"] = len(train)
    stats["PIR 別の要点 valid"] = len(valid)
    return train, valid


def load_triage_relabel(path: Path | None) -> dict[str, dict[str, str]]:
    """例の鍵 → {"importance", "reason"}。鍵は _example_key と同じ。"""
    if path is None or not path.exists():
        return {}
    data: dict[str, dict[str, str]] = json.loads(path.read_text(encoding="utf-8"))
    return data


def fix_example(
    r: dict[str, Any],
    stats: Counter[str],
    ttp: dict[str, dict[str, dict[str, Any]]] | None = None,
    triage: dict[str, dict[str, str]] | None = None,
    *,
    with_evidence: bool = False,
) -> dict[str, Any] | None:
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
    if t == "triage" and triage and (fix := triage.get(_example_key(r))):
        target = json.loads(msgs[-1]["content"])
        stats[f"triage 付け直し {target.get('importance')}→{fix['importance']}"] += 1
        target = {**target, "importance": fix["importance"], "reason": fix["reason"]}
        msgs[-1]["content"] = json.dumps(target, ensure_ascii=False)
    if t == "summary":
        target = json.loads(msgs[-1]["content"])
        dropped = [k for k in SUPPRESSED if k in target]
        target = {k: v for k, v in target.items() if k not in SUPPRESSED}
        if target.get("event_date") is None and target.get("event_date_basis") is not None:
            # 書き直しで日付だけ消え根拠の欄が残った例 (教師は入力に無い収穫日を
            # 「記事公開日」と書いていた)。日付が無いのに根拠がある形を学ばせない
            target["event_date_basis"] = None
            stats["日付なしの根拠の欄を空にした"] += 1
        verdicts = ttp.get(_example_key(r), {}) if ttp else {}
        if ttp and target.get("mitre_techniques"):
            target["mitre_techniques"] = clean_techniques(
                target["mitre_techniques"], verdicts, stats
            )
        if with_evidence:
            # 欄は最後 (本番のスキーマで最後の property)。技術は引用のあるものに絞り、
            # 技術と引用の組を 1 対 1 に保つ
            evidence = evidence_for(target.get("mitre_techniques") or [], verdicts, stats)
            target["mitre_techniques"] = [e["technique"] for e in evidence]
            target["mitre_evidence"] = evidence
            stats["引用欄つきの要約"] += 1
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
            ext: dict[str, Any] = next(
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


def _with_completion(
    r: dict[str, Any], completion: str | None, stats: Counter[str]
) -> dict[str, Any]:
    """書き直した教師の出力に差し替えた新しい例 (無ければそのまま)。"""
    if completion is None:
        return r
    stats["要約の教師を書き直し版に差し替え"] += 1
    msgs = [*r["messages"][:-1], {"role": "assistant", "content": completion}]
    return {**r, "messages": msgs}


def build(
    src: Path,
    knowledge: Path,
    out: Path,
    ttp_judged: Path | None = None,
    triage_relabel: Path | None = None,
    summary_corrected: Path | None = None,
    *,
    with_evidence: bool = False,
    pir_focus: Path | None = None,
) -> Counter[str]:
    stats: Counter[str] = Counter()
    rng = random.Random(SEED)
    ttp = load_ttp_verdicts(ttp_judged)
    triage = load_triage_relabel(triage_relabel)
    corrected = load_summary_corrected(summary_corrected)
    focus_train, focus_valid = pir_focus_examples(pir_focus, stats)
    out.mkdir(parents=True, exist_ok=True)
    for split in ("train", "valid"):
        rows = [json.loads(x) for x in (src / f"{split}.jsonl").open() if x.strip()]
        if split == "train" and corrected:
            rows = [_with_completion(r, corrected.get(i), stats) for i, r in enumerate(rows)]
        fixed = [
            e
            for e in (fix_example(r, stats, ttp, triage, with_evidence=with_evidence) for r in rows)
            if e is not None
        ]
        if split == "train":
            pairs = [e for e in fixed if task(e) == "pair"]
            others = [e for e in fixed if task(e) != "pair"]
            keep = rng.sample(pairs, min(PAIR_KEEP, len(pairs)))
            stats["pair を減らした"] = len(pairs) - len(keep)
            fixed = others + keep + knowledge_examples(knowledge, stats) + focus_train
            rng.shuffle(fixed)
        else:
            fixed = fixed + focus_valid
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
    ap.add_argument("--ttp-judged", type=Path, help="judge_teacher_ttp.py の出力 (技術の掃除)")
    ap.add_argument("--triage-relabel", type=Path, help="build_triage_relabel.py の出力")
    ap.add_argument(
        "--summary-corrected", type=Path, help="correct_summary_teacher.py の出力 (train の行番号)"
    )
    ap.add_argument(
        "--mitre-evidence",
        action="store_true",
        help="要約に技術ごとの原文の引用の欄を足す (s22 以降、--ttp-judged 必須)",
    )
    ap.add_argument(
        "--pir-focus", type=Path, help="build_sft_teacher_pir_focus.py の出力 (s22 以降)"
    )
    args = ap.parse_args()
    if args.mitre_evidence and args.ttp_judged is None:
        ap.error("--mitre-evidence には --ttp-judged が要る")
    built = build(
        args.src,
        args.knowledge,
        args.out,
        args.ttp_judged,
        args.triage_relabel,
        args.summary_corrected,
        with_evidence=args.mitre_evidence,
        pir_focus=args.pir_focus,
    )
    for k, v in sorted(built.items()):
        print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
