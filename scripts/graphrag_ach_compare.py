#!/usr/bin/env python3
"""GraphRAG の効果を ACH (情勢台帳・常設の問い) で測る — 学習なし・Opus の対読 (2026-09-29)。

Spotlight では線でたどった候補外の関連事象が 12:3 で効いた。ACH は s 系 (s22) の課題で、効くなら
本番の実装と教師の作り直しが要る (docs/research/llm_training/next_models_s22_n20.md §8)。

- 腕 A: 凍結の本番プロンプト (data/mlx/teacher/ach_opus.jsonl、key = 情勢 id:日付)
- 腕 B: 腕 A + 「証拠記事から線でたどった関連事象」の節 (【手順】の直前)。証拠記事は台帳の
  situation_evidence (その日付までに足されたもの)。線は本番の関係表示と同じ導出で、日付より後に
  報じられた事象は除く。**証拠台帳の index には使えない参考の文脈**と明記する
- 書き手・審判とも Opus。常設の問いを優先して選ぶ

    docker compose run --rm --no-deps -T -e LLM_LOCAL_FALLBACK=0 \\
        -v $PWD/src:/app/src:ro -v $PWD/scripts:/app/scripts:ro -v $PWD/data:/app/data \\
        kuebiko python scripts/graphrag_ach_compare.py --n 15
    (続けて --judge)
出力: data/mlx/graphrag_ach.jsonl / graphrag_ach_judge.jsonl (再開可能)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from pydantic import BaseModel, ConfigDict  # noqa: E402

from src.config_loader import load_app_config  # noqa: E402
from src.spotlight.graph_context import RELATION_DAYS  # noqa: E402
from src.synthesis.grounded.passes import _WireAnalysis  # noqa: E402
from src.tools.model_tiers import Step, build_llm_for_ref  # noqa: E402

D = _ROOT / "data" / "mlx"
TEACHER = D / "teacher" / "ach_opus.jsonl"
OUT = D / "graphrag_ach.jsonl"
JUDGE_OUT = D / "graphrag_ach_judge.jsonl"
_MAX_LINES = 20
_SEED = 929
_REL_LABELS = {
    "follow_up": "続報",
    "incident": "同じ出来事の関連",
    "contains": "包含 (まとめ記事)",
    "same_actor": "同じアクター",
}
_BASIS = {"victim": "被害組織", "cve": "CVE", "cap": "道具", "actor": "攻撃者"}
_NOTE = (
    "上の線は、事象が共有する指標から機械的に導いた参考の文脈である。**証拠台帳の index には"
    "使えない** (index は【ソース本文】の番号だけ)。線の先の事象を判定の根拠として数えず、"
    "線に無い関係は推測しない。前例・パターン・継続性を読む手がかりとしてのみ使う。"
)


def _evidence_articles(repo: Any, sid: str, until: datetime) -> list[tuple[str, str]]:
    with repo._connect() as conn:  # noqa: SLF001 — 読み取りのみ
        rows = conn.execute(
            "SELECT DISTINCT e.article_id, a.title FROM situation_evidence e "
            "JOIN articles a ON a.article_id = e.article_id "
            "WHERE e.situation_id = ? AND e.added_at < ?",
            (sid, until.isoformat()),
        ).fetchall()
    return [(str(r[0]), str(r[1] or "")) for r in rows]


def graph_block(repo: Any, rels: dict[str, list[Any]], sid: str, until: datetime) -> str:
    from src.cti.actor_normalizer import load_actor_aliases

    arts = _evidence_articles(repo, sid, until)
    if not arts:
        return ""
    title = dict(arts)
    ids = list(title)
    marks = ",".join("?" for _ in ids)
    with repo._connect() as conn:  # noqa: SLF001
        mem = conn.execute(
            "SELECT m.article_id, m.item_id FROM event_item_members m JOIN event_items i "
            f"ON i.id = m.item_id WHERE m.article_id IN ({marks}) AND i.merged_into IS NULL",
            tuple(ids),
        ).fetchall()
        first = {
            str(r[0]): datetime.fromisoformat(str(r[1]).replace("Z", "+00:00"))
            for r in conn.execute("SELECT id, first_reported_at FROM event_items").fetchall()
        }
    ev_title: dict[str, str] = {}
    for aid, ev in mem:
        ev_title.setdefault(str(ev), title[str(aid)])
    aliases = load_actor_aliases()

    def label(b: str) -> str:
        kind, _, v = b.partition(":")
        if kind == "actor":
            a = aliases.by_id(v)
            v = a.canonical if a is not None else v
        return f"{_BASIS.get(kind, kind)}: {v}"

    found: list[tuple[int, float, str]] = []
    seen: set[tuple[str, str]] = set()
    for ev in ev_title:
        for r in rels.get(ev, []):
            other = r.b if r.a == ev else r.a
            f = first.get(other)
            if other in ev_title or f is None or f >= until or (ev, other) in seen:
                continue
            seen.add((ev, other))
            head = repo.latest_event_versions([other]).get(other)
            if head is None or not head.headline:
                continue
            basis = "、".join(label(b) for b in r.basis) or "要約の類似"
            line = (
                f"- 証拠記事「{ev_title[ev][:60]}」↔ 関連事象「{head.headline}」"
                f"({f.date().isoformat()}): "
                f"{_REL_LABELS.get(r.rel_type, r.rel_type)} (共有: {basis})"
            )
            found.append((1 if r.rel_type == "same_actor" else 0, -f.timestamp(), line))
    if not found:
        return ""
    found.sort(key=lambda t: (t[0], t[1]))
    lines = [t[2] for t in found[:_MAX_LINES]]
    return (
        f"【線でたどった関連事象】({len(lines)} 本 / 全 {len(found)} 本)\n"
        + "\n".join(lines)
        + f"\n{_NOTE}\n\n"
    )


def with_block(prompt: str, block: str) -> str:
    at = prompt.find("【手順】")
    return prompt + "\n\n" + block if at < 0 else prompt[:at] + block + prompt[at:]


def _pick(rows: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """常設の問いを先に、残りは情勢を重複させずに無作為に。"""
    rng = random.Random(_SEED)
    standing = [r for r in rows if r["key"].startswith("s-standing")]
    others = [r for r in rows if not r["key"].startswith("s-standing")]
    rng.shuffle(standing)
    rng.shuffle(others)
    out: list[dict[str, Any]] = []
    used: set[str] = set()
    for r in standing + others:
        sid = r["key"].split(":")[0]
        if sid not in used:
            used.add(sid)
            out.append(r)
    return out[: n * 4]


async def generate(args: argparse.Namespace) -> int:
    from src.eventnews.relations import relations_by_event
    from src.storage.run_history import RunHistoryRepository

    repo = RunHistoryRepository()
    rels = relations_by_event(repo, days=RELATION_DAYS)
    rows = [json.loads(x) for x in TEACHER.open(encoding="utf-8")]
    done = {json.loads(x)["key"] for x in OUT.open()} if OUT.exists() else set()
    llm = build_llm_for_ref(args.model, Step.SYNTHESIS_ANALYSIS, load_app_config())
    todo: list[tuple[str, str, str]] = []
    thin = 0
    for r in _pick(rows, args.n):
        if len(todo) >= args.n:
            break
        sid, day = r["key"].split(":")
        until = datetime.fromisoformat(day).replace(tzinfo=UTC) + timedelta(days=1)
        block = graph_block(repo, rels, sid, until)
        if block.count("\n- ") < args.min_lines:
            thin += 1
            continue
        if r["key"] not in done:
            todo.append((r["key"], r["prompt"], with_block(r["prompt"], block)))
    print(f"線が {args.min_lines} 本未満 {thin} 件を飛ばした / 今回 {len(todo)} 件", flush=True)
    sem = asyncio.Semaphore(2)

    async def arm(prompt: str) -> str | None:
        async with sem:
            try:
                out = await llm.generate_structured(
                    prompt, _WireAnalysis, temperature=0.0, max_tokens=8000, think=False
                )
            except Exception as exc:  # noqa: BLE001
                print("FAIL", type(exc).__name__, str(exc)[:120], flush=True)
                return None
        return out.model_dump_json()

    with OUT.open("a", encoding="utf-8") as fh:
        for key, a_p, b_p in todo:
            a, b = await asyncio.gather(arm(a_p), arm(b_p))
            if a and b:
                fh.write(
                    json.dumps(
                        {"key": key, "prompt_a": a_p, "prompt_b": b_p, "a": a, "b": b},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                fh.flush()
                print("済", key, flush=True)
    return 0


JUDGE = """あなたは CTI 部門の上級分析官です。同じ情勢・同じソースに対する 2 つの ACH (競合仮説分析)
の結果を比べてください。参考として、ソースと、事象どうしの関係 (共有する指標から機械的に導いた線) を
示します。線は一方の分析者にしか渡されていません。

観点:
1. 判定の妥当性: leading 仮説と確度が、ソースの証拠に照らして妥当か (過確信・過小評価が無いか)
2. 根拠: 証拠台帳がソース本文に基づき、線の先の事象を証拠として数えていないか・線に無い関係を
   推測していないか (これがある方は大きく減点)
3. 前例・継続性の読み: 過去の関連事象を踏まえ、新規か継続かを正しく捉えているか
4. 分からないこと・監視指標が具体的か

各観点で優れた方 (X / Y / 同等) を選び、総合の勝者を決め、理由を 2-3 文で具体的に書く。

## ソースと線 (参考)
{context}

## 出力 X
{x}

## 出力 Y
{y}
"""


class Verdict(BaseModel):
    model_config = ConfigDict(extra="ignore")
    judgment: Literal["X", "Y", "同等"]
    grounding: Literal["X", "Y", "同等"]
    continuity: Literal["X", "Y", "同等"]
    unknowns: Literal["X", "Y", "同等"]
    winner: Literal["X", "Y", "同等"]
    reason: str


def _arm(s: str, *, b_is_x: bool) -> str:
    if s == "同等":
        return s
    return "B" if (s == "X") == b_is_x else "A"


async def judge(args: argparse.Namespace) -> int:
    rows = [json.loads(x) for x in OUT.open(encoding="utf-8")]
    done = {json.loads(x)["key"] for x in JUDGE_OUT.open()} if JUDGE_OUT.exists() else set()
    llm = build_llm_for_ref(args.judge_model, Step.PAIR_JUDGE, load_app_config())
    rng = random.Random(_SEED)
    with JUDGE_OUT.open("a", encoding="utf-8") as fh:
        for r in rows:
            flip = rng.random() < 0.5
            if r["key"] in done:
                continue
            x, y = (r["b"], r["a"]) if flip else (r["a"], r["b"])
            ctx = r["prompt_b"][:30000]
            try:
                v = await llm.generate_structured(
                    JUDGE.format(context=ctx, x=x, y=y), Verdict, temperature=0.0, max_tokens=2000
                )
            except Exception as exc:  # noqa: BLE001
                print("FAIL", r["key"], type(exc).__name__, flush=True)
                continue
            out = {
                "key": r["key"],
                **{
                    k: _arm(getattr(v, k), b_is_x=flip)
                    for k in ("winner", "judgment", "grounding", "continuity", "unknowns")
                },
                "reason": v.reason,
            }
            fh.write(json.dumps(out, ensure_ascii=False) + "\n")
            fh.flush()
            print(r["key"], "勝者", out["winner"], flush=True)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", default="claudecode:opus")
    p.add_argument("--judge-model", default="claudecode:opus")
    p.add_argument("--n", type=int, default=15)
    p.add_argument("--min-lines", type=int, default=3)
    p.add_argument("--judge", action="store_true")
    a = p.parse_args()
    return asyncio.run(judge(a) if a.judge else generate(a))


if __name__ == "__main__":
    raise SystemExit(main())
