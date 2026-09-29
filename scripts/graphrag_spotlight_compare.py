#!/usr/bin/env python3
"""GraphRAG の効果を Spotlight で測る — 学習なし・Opus の対読 (2026-09-29)。

問い: 事象どうしの線 (共有する指標から決定論で導いた関係) を入力に足すと、Spotlight は
良くなるか。GraphRAG が勝つのは多段・集約の問いだけ (GraphRAG-Bench) なので、PIR を縦に
読む Spotlight (7 日窓・複数事象の横断) で測る。**効くと分かってから** n20 の教師をその入力で
作り直す (docs/research/llm_training/next_models_s22_n20.md §4)。

- 腕 A: 本番のプロンプト (凍結窓 = eval_spotlight_frozen.py と同じ選び方・本番の文面)
- 腕 B: 腕 A + 「事象どうしの関係」の節。線は ``relations_by_event`` (本番の関係表示と同じ)
  から、窓の記事が属する事象の間のものだけ。**線ごとに種類・共有した指標・出典の記事番号**を付け、
  線に無い関係を推測しないよう 1 文添える
- 書き手はどちらも Opus (学習なし)。審判は盲検の対読 (左右を乱数で入れ替え)

⚠ 事象と関係は **今の** 状態から引く。窓より後に報じられた事象は線から除く (未来の混入を防ぐ)。

    docker compose run --rm --no-deps -T -e LLM_LOCAL_FALLBACK=0 \\
        -v $PWD/src:/app/src:ro -v $PWD/scripts:/app/scripts:ro -v $PWD/data:/app/data \\
        kuebiko python scripts/graphrag_spotlight_compare.py --n 15
    (続けて --judge で対読)
出力: data/mlx/graphrag_spotlight.jsonl / graphrag_spotlight_judge.jsonl (再開可能)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

from eval_spotlight_frozen import TEACHER, graft_v31, pick  # noqa: E402
from pydantic import BaseModel, ConfigDict  # noqa: E402

from src.config_loader import load_app_config  # noqa: E402
from src.spotlight.generator import _LLMSpotlightOutput  # noqa: E402
from src.tools.llm_client import LLMClient  # noqa: E402
from src.tools.model_tiers import Step, build_llm_for_ref  # noqa: E402

D = Path("/app/data/mlx") if Path("/app/data/mlx").exists() else _ROOT / "data" / "mlx"
OUT = D / "graphrag_spotlight.jsonl"
JUDGE_OUT = D / "graphrag_spotlight_judge.jsonl"
_REL_DAYS = 120
_MAX_LINES = 25
_SEED = 929
_REL_LABELS = {
    "follow_up": "続報",
    "incident": "同じ出来事の関連",
    "side": "同じ出来事の別の側面",
    "campaign": "同一キャンペーン",
    "contains": "包含 (まとめ記事)",
    "same_actor": "同じアクター",
}
_BASIS = {"victim": "被害組織", "cve": "CVE", "cap": "道具", "actor": "攻撃者"}
_PERIOD = re.compile(r"(\d{4}-\d{2}-\d{2}) 〜 (\d{4}-\d{2}-\d{2})")
_ARTICLE = re.compile(r"^\[(\d+)\] .*?\n\s+article_id: (\S+)", re.M)
_NOTE = (
    "上の線は、事象が共有する指標 (被害組織・CVE・道具・攻撃者) から機械的に導いたもの"
    "である。書くときは線を根拠として引用してよいが、線に無い事象どうしの関係は推測しない。"
)


# ---------- 線の節 ----------


def _window(prompt: str) -> tuple[datetime, datetime] | None:
    m = _PERIOD.search(prompt)
    if not m:
        return None
    start = datetime.fromisoformat(m.group(1)).replace(tzinfo=UTC)
    return start, datetime.fromisoformat(m.group(2)).replace(tzinfo=UTC) + timedelta(days=1)


def _members(repo: Any, article_ids: list[str]) -> dict[str, list[str]]:
    """記事 id → 属する事象 id。"""
    if not article_ids:
        return {}
    marks = ",".join("?" for _ in article_ids)
    with repo._connect() as conn:  # noqa: SLF001 — 読み取りのみ
        rows = conn.execute(
            "SELECT m.article_id, m.item_id FROM event_item_members m "
            "JOIN event_items i ON i.id = m.item_id "
            f"WHERE m.article_id IN ({marks}) AND i.merged_into IS NULL",
            tuple(article_ids),
        ).fetchall()
    out: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        out[str(r[0])].append(str(r[1]))
    return out


def _basis_label(basis: str, canonical: Any) -> str:
    kind, _, value = basis.partition(":")
    if kind == "actor":
        actor = canonical(value)
        value = actor.canonical if actor is not None else value
    return f"{_BASIS.get(kind, kind)}: {value}"


def _headline(repo: Any, item_id: str) -> str:
    v = repo.latest_event_versions([item_id]).get(item_id)
    return str(v.headline) if v is not None and v.headline else ""


def graph_block(prompt: str, repo: Any, rels: dict[str, list[Any]], first: dict[str, Any]) -> str:
    """窓の記事が属する事象から線をたどった関連事象 (決定論)。線が無ければ空文字。

    候補どうしの線はほぼ無い (1 窓の 30 件はほぼ別々の事象、09-29 実測)。線の大半は候補の
    **外** の事象へ伸びるので、たどった先 (窓の終わりまでに報じられた事象) を見出しと日付つきで
    渡す = GraphRAG の取得。同じアクターより出来事の線 (続報・関連・包含) を先に、新しい順に。
    """
    from src.cti.actor_normalizer import load_actor_aliases

    win = _window(prompt)
    if win is None:
        return ""
    idx = {aid: n for n, aid in _ARTICLE.findall(prompt)}
    by_article = _members(repo, list(idx))
    events: dict[str, list[str]] = defaultdict(list)  # 事象 → 記事番号
    for aid, items in by_article.items():
        for it in items:
            events[it].append(idx[aid])
    aliases = load_actor_aliases()
    seen: set[tuple[str, str, str]] = set()
    found: list[tuple[int, datetime, str]] = []
    for ev in events:
        for r in rels.get(ev, []):
            other = r.b if r.a == ev else r.a
            f = first.get(other)
            if other == ev or f is None or f >= win[1]:
                continue
            key = (min(ev, other), max(ev, other), r.rel_type)
            if key in seen:
                continue
            seen.add(key)
            basis = "、".join(_basis_label(b, aliases.by_id) for b in r.basis) or "要約の類似"
            label = _REL_LABELS.get(r.rel_type, r.rel_type)
            a_ref = "".join(f"[{n}]" for n in sorted(set(events[ev]), key=int))
            if other in events:
                b_ref = "記事 " + "".join(f"[{n}]" for n in sorted(set(events[other]), key=int))
            else:
                head = _headline(repo, other)
                if not head:
                    continue
                b_ref = f"候補外の事象「{head}」({f.date().isoformat()})"
            rank = 1 if r.rel_type == "same_actor" else 0
            found.append((rank, f, f"- 記事 {a_ref} ↔ {b_ref}: {label} (共有: {basis})"))
    if not found:
        return ""
    found.sort(key=lambda t: (t[0], -t[1].timestamp()))
    lines = [t[2] for t in found[:_MAX_LINES]]
    return (
        f"## 線でたどった関連事象 ({len(lines)} 本 / 全 {len(found)} 本)\n"
        + "\n".join(lines)
        + f"\n\n{_NOTE}\n\n"
    )


def with_block(prompt: str, block: str) -> str:
    """記事一覧の直後 (次の節の見出しの前) に線の節を差し込む。"""
    head = prompt.find("## この PIR にマッチした")
    nxt = prompt.find("\n## ", head + 5) if head >= 0 else -1
    if nxt < 0:
        return prompt + "\n\n" + block
    return prompt[: nxt + 1] + block + prompt[nxt + 1 :]


# ---------- 生成 ----------


async def generate(args: argparse.Namespace) -> int:
    from src.eventnews.relations import relations_by_event
    from src.storage.run_history import RunHistoryRepository

    repo = RunHistoryRepository()
    rels = relations_by_event(repo, days=_REL_DAYS)
    with repo._connect() as conn:  # noqa: SLF001
        first = {
            str(r[0]): datetime.fromisoformat(str(r[1]).replace("Z", "+00:00"))
            for r in conn.execute("SELECT id, first_reported_at FROM event_items").fetchall()
        }
    rows = [json.loads(x) for x in TEACHER.open(encoding="utf-8")]
    done = {json.loads(x)["key"] for x in OUT.open()} if OUT.exists() else set()
    llm = build_llm_for_ref(
        args.model, Step.PIR_SPOTLIGHT, load_app_config(), timeout_seconds=1800.0
    )
    todo: list[tuple[str, str, str]] = []
    no_lines = 0
    for r in pick(rows, args.pool):
        if len(todo) >= args.n:
            break
        prompt, _ = graft_v31(r["prompt"])
        block = graph_block(prompt, repo, rels, first)
        if block.count("\n- ") < args.min_lines:
            no_lines += 1
            continue
        if r["key"] not in done:
            todo.append((r["key"], prompt, with_block(prompt, block)))
    print(
        f"線が {args.min_lines} 本未満の窓 {no_lines} を飛ばした / 今回 {len(todo)} 窓", flush=True
    )
    sem = asyncio.Semaphore(2)

    async def arm(prompt: str) -> str | None:
        async with sem:
            try:
                out = await llm.generate_structured(
                    prompt=prompt, schema=_LLMSpotlightOutput, temperature=0.0, max_tokens=8000
                )
            except Exception as exc:  # noqa: BLE001
                print("FAIL", type(exc).__name__, str(exc)[:120], flush=True)
                return None
        return out.model_dump_json()

    with OUT.open("a", encoding="utf-8") as fh:
        for key, a_prompt, b_prompt in todo:
            a, b = await asyncio.gather(arm(a_prompt), arm(b_prompt))
            if a and b:
                row = {"key": key, "prompt_a": a_prompt, "prompt_b": b_prompt, "a": a, "b": b}
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
                print("済", key, flush=True)
    return 0


# ---------- 対読 ----------

JUDGE = """あなたは CTI 部門の上級分析官です。同じ PIR・同じ記事群から書かれた 2 つの週次の縦断分析
(Spotlight) を比べてください。参考として、記事一覧と、事象どうしの関係 (共有する指標から機械的に
導いた線) を示します。線は一方の書き手にしか渡されていません。

次の観点で、読み手 (日本の CTI 担当者) の判断に役立つのはどちらかを決めてください。
1. 事象どうしのつながり (同一キャンペーン・続報・同じアクター) を正しく捉え、個別の記事の要約を
   超えた全体像を示しているか
2. 記事と線に根拠の無い主張・関係の推測・確度の格上げが無いか (これがある方は大きく減点)
3. 見通し・留保・分からないことが具体的か

各観点で優れた方 (X / Y / 同等) を選び、最後に総合の勝者を決め、理由を 2-3 文で書く。
勝敗の理由には、出力の該当箇所を具体的に挙げること。

## 記事一覧と線 (参考)
{context}

## 出力 X
{x}

## 出力 Y
{y}
"""


def _arm(s: str, *, b_is_x: bool) -> str:
    """盲検の X / Y を腕 A / B へ戻す。"""
    if s == "同等":
        return s
    return "B" if (s == "X") == b_is_x else "A"


class Verdict(BaseModel):
    model_config = ConfigDict(extra="ignore")
    connections: Literal["X", "Y", "同等"]
    grounding: Literal["X", "Y", "同等"]
    outlook: Literal["X", "Y", "同等"]
    winner: Literal["X", "Y", "同等"]
    reason: str


async def judge(args: argparse.Namespace) -> int:
    rows = [json.loads(x) for x in OUT.open(encoding="utf-8")]
    done = {json.loads(x)["key"] for x in JUDGE_OUT.open()} if JUDGE_OUT.exists() else set()
    llm: LLMClient = build_llm_for_ref(args.judge_model, Step.PAIR_JUDGE, load_app_config())
    rng = random.Random(_SEED)
    with JUDGE_OUT.open("a", encoding="utf-8") as fh:
        for r in rows:
            flip = rng.random() < 0.5
            if r["key"] in done:
                continue
            x, y = (r["b"], r["a"]) if flip else (r["a"], r["b"])
            ctx = r["prompt_b"][r["prompt_b"].find("## この PIR にマッチした") :][:30000]
            try:
                v = await llm.generate_structured(
                    prompt=JUDGE.format(context=ctx, x=x, y=y),
                    schema=Verdict,
                    temperature=0.0,
                    max_tokens=2000,
                )
            except Exception as exc:  # noqa: BLE001
                print("FAIL", r["key"], type(exc).__name__, flush=True)
                continue

            def arm(s: str, b_is_x: bool = flip) -> str:
                return _arm(s, b_is_x=b_is_x)

            out = {
                "key": r["key"],
                "winner": arm(v.winner),
                "connections": arm(v.connections),
                "grounding": arm(v.grounding),
                "outlook": arm(v.outlook),
                "reason": v.reason,
                "b_is_x": flip,
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
    p.add_argument("--pool", type=int, default=60, help="線の有無を調べる窓の数")
    p.add_argument("--min-lines", type=int, default=3, help="線がこれ未満の窓は比べない")
    p.add_argument("--judge", action="store_true")
    a = p.parse_args()
    return asyncio.run(judge(a) if a.judge else generate(a))


if __name__ == "__main__":
    raise SystemExit(main())
