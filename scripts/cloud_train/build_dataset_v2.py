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


def load_flagged(path: Path | None) -> set[int]:
    """根拠監査で問題を指摘された train の行番号 (judge_n_teacher_grounding.py の出力)。"""
    if path is None or not path.exists():
        return set()
    rows = (json.loads(x) for x in path.open(encoding="utf-8") if x.strip())
    return {int(r["i"]) for r in rows if r.get("issues")}


def load_rules_fix(paths: Path | list[Path] | None) -> dict[str, str | None]:
    """例の鍵 → 本番の指示に照らして直した最終形 (違反なしは含めない。直せなかったら None)。

    fix_summary_rules.py / shorten_summaries.py の出力。鍵は _example_key と同じで、
    **入力の改訂 (PROMPT_REWRITES) の前**の文字列から計算する。後のファイルが前を上書きする。
    """
    out: dict[str, str | None] = {}
    for path in [paths] if isinstance(paths, Path) else paths or []:
        if not path.exists():
            continue
        for row in (json.loads(x) for x in path.open(encoding="utf-8") if x.strip()):
            if row.get("issues"):
                out[str(row["key"])] = row.get("corrected") or None
    return out


def _pre_rewrite_key(e: dict[str, Any]) -> str:
    """指示の改訂 (PROMPT_REWRITES) を戻した入力から鍵を作る (修正結果は改訂前に作ったため)。"""
    t = task(e)
    msgs = []
    for m in e["messages"]:
        content = m["content"]
        if m["role"] != "assistant":
            for old, new in PROMPT_REWRITES.get(t, ()):
                content = content.replace(new, old)
        msgs.append({**m, "content": content})
    return _example_key({"messages": msgs})


def apply_rules_fix(
    examples: list[Any], fixes: dict[str, str | None], stats: Counter[str]
) -> list[Any]:
    """違反を直した例は置き換え、直せなかった例は外す (新しいリストを返す)。"""
    out = []
    for e in examples:
        fix = fixes.get(_pre_rewrite_key(e), "") if task(e) == "summary" else ""
        if fix == "":
            out.append(e)
        elif fix is None:
            stats["要約 指示違反を直せず外した"] += 1
        else:
            stats["要約 指示違反を直した"] += 1
            out.append({"messages": [*e["messages"][:-1], {"role": "assistant", "content": fix}]})
    return out


def load_summary_corrected(path: Path | None) -> dict[int, str]:
    """train の行番号 → 書き直した要約 (correct_summary_teacher.py の出力)。"""
    if path is None or not path.exists():
        return {}
    return {
        int(row["i"]): str(row["completion"])
        for row in (json.loads(x) for x in path.open(encoding="utf-8") if x.strip())
    }


#: 接頭辞 (src/tools/task_prefix.py の TASK_MARKERS と同じ)
PIR_FOCUS_MARKER = "[task: pir_focus]\n"
AXES_MARKER = "[task: axes]\n"
#: 検証に回す割合 (学習と重ならない)
PIR_FOCUS_VALID_EVERY = 20


def pir_focus_examples(path: Path | None, stats: Counter[str]) -> tuple[list[Any], list[Any]]:
    """PIR 別の要点の教師対 (build_sft_teacher_pir_focus.py) → (train, valid) の例。"""
    return teacher_examples(path, PIR_FOCUS_MARKER, "PIR 別の要点", stats)


def teacher_examples(
    path: Path | None, marker: str, label: str, stats: Counter[str]
) -> tuple[list[Any], list[Any]]:
    """{key, prompt, completion} の教師対 → (train, valid) の例。

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
                {"role": "user", "content": marker + row["prompt"]},
                {"role": "assistant", "content": text},
            ]
        }
        (valid if i % PIR_FOCUS_VALID_EVERY == 0 else train).append(ex)
    stats[f"{label} train"] = len(train)
    stats[f"{label} valid"] = len(valid)
    return train, valid


#: 要約の長さの指示 (2026-09-30 利用者判断: 字数でなく内容で — 重要事項を漏らさず冗長にしない)
SUMMARY_RULE_1 = "**日本語要約 (1〜4 段落)。長さは字数でなく内容で決める**。"
SUMMARY_RULE_2 = (
    "重要事項 (何が起きたか・誰が — 帰属とその確度・何が狙われ影響は何か・時期・留保と但し書き・"
    "対処) を漏らさず、重複・一般論・記事に無い背景は書かない。単純な事象は短く。"
)

#: 本番の指示の改訂に学習データの入力を揃える (学習と本番の入力の形を一致させる)。
#: (旧, 新) の組。要約の長さは 2026-09-30 に利用者判断で 250〜500 → 300〜700 字
PROMPT_REWRITES: dict[str, tuple[tuple[str, str], ...]] = {
    "summary": (
        (
            "**2〜3 段落の日本語要約。全体で 250〜500 字に収める**。"
            "\n  技術的詳細を含めつつ、冗長な記述は避ける。",
            f"{SUMMARY_RULE_1}\n  {SUMMARY_RULE_2}",
        ),
    ),
}


def rewrite_prompt(content: str, task_name: str, stats: Counter[str]) -> str:
    """本番の指示の改訂を学習データの入力へ反映した新しい文字列。"""
    for old, new in PROMPT_REWRITES.get(task_name, ()):
        if old in content:
            content = content.replace(old, new)
            stats[f"指示の改訂を反映 {task_name}"] += 1
    return content


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
    msgs = [
        {**m, "content": rewrite_prompt(m["content"], t, stats)}
        if m["role"] != "assistant"
        else dict(m)
        for m in r["messages"]
    ]
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
    summary_flagged: Path | None = None,
    *,
    with_evidence: bool = False,
    pir_focus: Path | None = None,
    axes_teacher: Path | None = None,
    rules_fix: Path | list[Path] | None = None,
) -> Counter[str]:
    stats: Counter[str] = Counter()
    rng = random.Random(SEED)
    ttp = load_ttp_verdicts(ttp_judged)
    triage = load_triage_relabel(triage_relabel)
    corrected = load_summary_corrected(summary_corrected)
    flagged = load_flagged(summary_flagged)
    fixes = load_rules_fix(rules_fix)
    focus_train, focus_valid = pir_focus_examples(pir_focus, stats)
    axes_train, axes_valid = teacher_examples(axes_teacher, AXES_MARKER, "深刻度の軸", stats)
    focus_train, focus_valid = focus_train + axes_train, focus_valid + axes_valid
    out.mkdir(parents=True, exist_ok=True)
    for split in ("train", "valid"):
        rows = [json.loads(x) for x in (src / f"{split}.jsonl").open() if x.strip()]
        if split == "train" and corrected:
            rows = [_with_completion(r, corrected.get(i), stats) for i, r in enumerate(rows)]
        if split == "train" and flagged:
            # 問題を指摘されたが書き直せなかった例は外す (量より質)
            kept = [r for i, r in enumerate(rows) if i not in flagged or i in corrected]
            stats["指摘されたが未修正で外した"] = len(rows) - len(kept)
            rows = kept
        fixed = [
            e
            for e in (fix_example(r, stats, ttp, triage, with_evidence=with_evidence) for r in rows)
            if e is not None
        ]
        if fixes:
            fixed = apply_rules_fix(fixed, fixes, stats)
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
        "--summary-flagged",
        type=Path,
        help="要約の根拠監査の出力。指摘されたが書き直せなかった例を外す",
    )
    ap.add_argument(
        "--mitre-evidence",
        action="store_true",
        help="要約に技術ごとの原文の引用の欄を足す (s22 以降、--ttp-judged 必須)",
    )
    ap.add_argument(
        "--pir-focus", type=Path, help="build_sft_teacher_pir_focus.py の出力 (s22 以降)"
    )
    ap.add_argument("--axes-teacher", type=Path, help="build_sft_teacher_axes.py の出力 (s22 以降)")
    ap.add_argument(
        "--summary-rules-fix",
        type=Path,
        action="append",
        help="fix_summary_rules.py / shorten_summaries.py の出力 (複数可、後が優先)",
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
        args.summary_flagged,
        with_evidence=args.mitre_evidence,
        pir_focus=args.pir_focus,
        axes_teacher=args.axes_teacher,
        rules_fix=args.summary_rules_fix,
    )
    for k, v in sorted(built.items()):
        print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
