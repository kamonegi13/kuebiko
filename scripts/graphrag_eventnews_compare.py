#!/usr/bin/env python3
"""GraphRAG の効果を事象ニュースの記事生成で測る — 学習なし・Opus の対読 (2026-10-08)。

Spotlight では線でたどった候補外の関連事象が 12:3 で効いた (graphrag_spotlight_compare.py)。
次は事象ニュース (``src/eventnews/generator.py:build_prompt`` — 統合記事を書く唯一の LLM 呼出)
で同じ問いを測る (docs/research/llm_training/next_models_s22_n20.md §8.7)。

**本番と同じ入力の形**: ``build_prompt(members, allowed_identifiers_text)`` は LLM 呼出なしで
プロンプトを組む純粋関数 (seam)。既存の事象 (``event_items`` / ``event_item_members``) から
メンバー記事を読み直し (読み取りのみ・再群化しない)、本番と同じ selectors
(``select_members`` / ``identifier_gate.render_allowed_identifiers``) を通してから渡す。

- 腕 A: 本番の refine プロンプト (``## 対象記事`` に番号付きで提示されたメンバー)
- 腕 B: 腕 A + 「線でたどった関連事象」の節 (``## 対象記事`` の直後、識別子カタログの前)。
  線は ``src.spotlight.graph_context.build_graph_context`` をそのまま再利用する — この事象
  自身の構成記事 id を渡すだけで、関係表示と同じ導出 (``relations_by_event``) で**候補外**の
  関連事象をたどる (Spotlight と同じ取得口を 1 関数呼出に閉じ込める — 後で src/graph/ の
  共通モジュールに差し替えるときもここだけ直せばよい)
- 書き手はどちらも既定 Opus (学習なし)。審判は盲検の対読 (Verdict は spotlight 版を再利用)

凍結窓は初回に ``data/eval/graphrag_eventnews_frozen.jsonl`` へ保存し、以後は再構築しない
(再実行で同じ入力)。

    docker exec kuebiko python scripts/graphrag_eventnews_compare.py --dry --n 15
    docker exec kuebiko python scripts/graphrag_eventnews_compare.py --n 15
    docker exec kuebiko python scripts/graphrag_eventnews_compare.py --judge
出力: data/eval/graphrag_eventnews_frozen.jsonl (凍結入力) /
      data/eval/graphrag_eventnews.jsonl (生成、再開可能) /
      data/eval/graphrag_eventnews_judge.jsonl (対読、再開可能)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

from graphrag_spotlight_compare import Verdict, _arm  # noqa: E402 — Verdict/盲検戻しを再利用
from graphrag_synthesis_compare import (  # noqa: E402 — 同じ機械検出を再利用
    ESCALATION_PHRASES,
    _escalation_hits,
)

from src.config_loader import load_app_config  # noqa: E402
from src.eventnews.generator import (  # noqa: E402
    EVENT_NEWS_MAX_TOKENS,
    build_prompt,
    select_members,
)
from src.eventnews.identifier_gate import render_allowed_identifiers  # noqa: E402
from src.eventnews.models import EventNewsDraft, MemberArticle  # noqa: E402
from src.spotlight.graph_context import build_graph_context  # noqa: E402
from src.storage.event_time import EVENT_TS_EXPR  # noqa: E402
from src.storage.repo_eventnews import EventItemRecord  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.tools.llm_client import LLMClient  # noqa: E402
from src.tools.model_tiers import Step, build_llm_for_ref  # noqa: E402

D = Path("/app/data/eval") if Path("/app/data/eval").exists() else _ROOT / "data" / "eval"
FROZEN = D / "graphrag_eventnews_frozen.jsonl"
OUT = D / "graphrag_eventnews.jsonl"
JUDGE_OUT = D / "graphrag_eventnews_judge.jsonl"

_SEED = 1008
_MIN_MEMBERS = 2  # 単独報は統合主張が無く線の効果を測りにくいので外す
_MEMBERS_HEADING = "## 対象記事"
_SQL_MEMBERS = (
    "SELECT a.article_id AS article_id,"
    f" {EVENT_TS_EXPR.format(a='a')} AS anchor_ts,"
    " a.importance AS importance, a.category AS category, a.status AS status,"
    " a.title AS title, a.url AS url,"
    " COALESCE(a.feed_title,'') AS feed_title, COALESCE(a.feed_url,'') AS feed_url,"
    " COALESCE(a.summary,'') AS summary, COALESCE(a.body,'') AS body"
    " FROM articles a WHERE a.article_id IN ({placeholders})"
)


# ---------- 既存アイテムの読み直し (再群化しない、読み取りのみ) ----------


def _fetch_members(repo: Any, article_ids: list[str]) -> dict[str, MemberArticle]:
    """構成記事を本文つきで読み直す (``src.ui.services.eventnews_hourly_job._load_members``
    と同じ列構成だが entities/account_class/kind は空 — ``build_prompt`` はどちらも読まない)。
    """
    if not article_ids:
        return {}
    placeholders = ",".join("?" for _ in article_ids)
    with repo._connect() as conn:  # noqa: SLF001 — 読み取りのみ
        rows = conn.execute(_SQL_MEMBERS.format(placeholders=placeholders), article_ids).fetchall()
    out: dict[str, MemberArticle] = {}
    for r in rows:
        anchor = r["anchor_ts"]
        anchor = anchor if isinstance(anchor, datetime) else datetime.fromisoformat(str(anchor))
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=UTC)
        aid = str(r["article_id"])
        out[aid] = MemberArticle(
            article_id=aid,
            title=str(r["title"]),
            url=str(r["url"]),
            feed_title=str(r["feed_title"]),
            feed_url=str(r["feed_url"]),
            host=str(r["url"]).split("/")[2] if "://" in str(r["url"]) else "",
            importance=str(r["importance"]),
            category=str(r["category"]),
            status=str(r["status"]),
            anchor_ts=anchor,
            summary=str(r["summary"]),
            body=str(r["body"]),
            entities=frozenset(),
        )
    return out


def _graph_block(repo: Any, article_ids: list[str], window_end: datetime) -> str:
    """線でたどった候補外の関連事象 (GraphRAG の取得)。Spotlight と同じ口を再利用する —
    差し替え時はこの 1 関数だけ直せばよい。"""
    return build_graph_context(repo, article_ids, window_end)


def with_block(prompt: str, block: str) -> str:
    """``## 対象記事`` の直後 (次の見出しの前) に線の節を差し込む。"""
    if not block:
        return prompt
    head = prompt.find(_MEMBERS_HEADING)
    nxt = prompt.find("\n## ", head + len(_MEMBERS_HEADING)) if head >= 0 else -1
    if nxt < 0:
        return prompt + "\n\n" + block
    return prompt[: nxt + 1] + block + prompt[nxt + 1 :]


# ---------- 窓の選定 (凍結。初回のみ構築し、以後は再構築しない) ----------


def _candidate_items(repo: RunHistoryRepository, *, pool: int) -> list[EventItemRecord]:
    return repo.list_event_items(
        exclude_merged=True,
        importances=("high", "medium"),
        order_by="recency",
        limit=pool,
    )


def _build_frozen(args: argparse.Namespace) -> list[dict[str, Any]]:
    repo = RunHistoryRepository()
    items = _candidate_items(repo, pool=args.pool)
    windows: list[dict[str, Any]] = []
    no_lines = 0
    for item in items:
        if len(windows) >= args.n:
            break
        member_ids = list(item.state.member_ids)
        if len(member_ids) < _MIN_MEMBERS or item.state.current_version < 1:
            continue
        members_by_id = _fetch_members(repo, member_ids)
        members = [members_by_id[aid] for aid in member_ids if aid in members_by_id]
        textual = [m for m in members if (m.body or m.summary).strip()]
        if len(textual) < _MIN_MEMBERS:
            continue
        selected, _omitted = select_members(textual)
        article_ids = [m.article_id for m in selected]
        window_end = item.state.last_reported_at
        block = _graph_block(repo, article_ids, window_end)
        n_lines = block.count("\n- ")
        if n_lines < args.min_lines:
            no_lines += 1
            continue
        allowed = render_allowed_identifiers(selected)
        prompt_a = build_prompt(selected, allowed)
        windows.append(
            {
                "key": f"ev:{item.state.item_id}",
                "prompt_a": prompt_a,
                "prompt_b": with_block(prompt_a, block),
                "n_lines": n_lines,
            }
        )
    print(
        f"線が {args.min_lines} 本未満の窓 {no_lines} を飛ばした / 今回 {len(windows)} 窓",
        flush=True,
    )
    return windows


def _load_or_build_frozen(args: argparse.Namespace) -> list[dict[str, Any]]:
    if FROZEN.exists() and not args.fresh:
        return [json.loads(x) for x in FROZEN.open(encoding="utf-8")]
    windows = _build_frozen(args)
    FROZEN.parent.mkdir(parents=True, exist_ok=True)
    with FROZEN.open("w", encoding="utf-8") as fh:
        for w in windows:
            fh.write(json.dumps(w, ensure_ascii=False) + "\n")
    return windows


# ---------- 生成 ----------


def _count_keys(path: Path) -> int:
    if not path.exists():
        return 0
    return len({json.loads(x)["key"] for x in path.open(encoding="utf-8") if x.strip()})


async def generate(args: argparse.Namespace) -> int:
    windows = _load_or_build_frozen(args)
    total_lines = sum(w["n_lines"] for w in windows)
    lengths = sorted(len(w["prompt_b"]) for w in windows)
    mid = lengths[len(lengths) // 2] if lengths else 0
    print(
        f"窓 {len(windows)} / 線 合計 {total_lines} 本 / "
        f"prompt_b 文字数 中央 {mid} 最大 {max(lengths, default=0)}",
        flush=True,
    )
    if args.dry:
        return 0
    done = {json.loads(x)["key"] for x in OUT.open()} if OUT.exists() else set()
    llm = build_llm_for_ref(args.model, Step.EVENT_NEWS, load_app_config(), timeout_seconds=600.0)
    sem = asyncio.Semaphore(2)

    async def arm(prompt: str) -> str | None:
        async with sem:
            try:
                out = await llm.generate_structured(
                    prompt=prompt,
                    schema=EventNewsDraft,
                    temperature=0.2,
                    max_tokens=EVENT_NEWS_MAX_TOKENS,
                    think=False,
                )
            except Exception as exc:  # noqa: BLE001
                print("FAIL", type(exc).__name__, str(exc)[:120], flush=True)
                return None
        return out.model_dump_json()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("a", encoding="utf-8") as fh:
        for w in windows:
            if w["key"] in done:
                continue
            a, b = await asyncio.gather(arm(w["prompt_a"]), arm(w["prompt_b"]))
            if a and b:
                row = {
                    "key": w["key"],
                    "prompt_a": w["prompt_a"],
                    "prompt_b": w["prompt_b"],
                    "a": a,
                    "b": b,
                }
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
                print("済", w["key"], flush=True)
    # 失敗した窓が残れば非 0 — run_until_done.sh が上限の解除後に残りだけ再開する
    return 0 if _count_keys(OUT) >= len(windows) else 1


# ---------- 対読 ----------

JUDGE = """あなたは CTI 部門の上級分析官です。同じ構成記事群から書かれた 2 つの事象ニュース
(複数記事を 1 本に統合した記事) を比べてください。参考として、対象記事一覧と、この事象から
線でたどった関連事象 (共有する指標から機械的に導いた線) を示します。線は一方の書き手にしか
渡されていません。

次の観点で、読み手 (日本の CTI 担当者) の判断に役立つのはどちらかを決めてください。
1. この事象と周辺の事象とのつながり (続報・同一キャンペーン・同じアクター) を正しく捉え、
   対象記事の要約を超えた文脈を示しているか
2. 対象記事・線に根拠の無い主張・関係の推測・確度の格上げが無いか (これがある方は大きく減点)
3. 相違点・未解明点が具体的で、読み手にとって有用か

各観点で優れた方 (X / Y / 同等) を選び、最後に総合の勝者を決め、理由を 2-3 文で書く。
勝敗の理由には、出力の該当箇所を具体的に挙げること。

## 対象記事一覧と線 (参考)
{context}

## 出力 X
{x}

## 出力 Y
{y}
"""


async def judge(args: argparse.Namespace) -> int:
    src = args.src or OUT
    judge_out = args.judge_out or JUDGE_OUT
    rows = [json.loads(x) for x in src.open(encoding="utf-8")]
    done = {json.loads(x)["key"] for x in judge_out.open()} if judge_out.exists() else set()
    llm: LLMClient = build_llm_for_ref(args.judge_model, Step.PAIR_JUDGE, load_app_config())
    rng = random.Random(_SEED)
    judge_out.parent.mkdir(parents=True, exist_ok=True)
    escalations: list[tuple[str, list[str]]] = []
    with judge_out.open("a", encoding="utf-8") as fh:
        for r in rows:
            flip = rng.random() < 0.5
            hits = _escalation_hits(r["a"], r["b"])
            if hits:
                escalations.append((r["key"], hits))
            if r["key"] in done:
                continue
            x, y = (r["b"], r["a"]) if flip else (r["a"], r["b"])
            at = r["prompt_b"].find(_MEMBERS_HEADING)
            ctx = r["prompt_b"][at if at >= 0 else 0 :][:30000]
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
                "escalation_phrases": hits,
            }
            fh.write(json.dumps(out, ensure_ascii=False) + "\n")
            fh.flush()
            print(r["key"], "勝者", out["winner"], flush=True)
    if escalations:
        print(f"\n線つき版にのみ出た格上げ語彙 ({ESCALATION_PHRASES}):")
        print(f"  {len(escalations)}/{len(rows)} 窓")
        for key, hits in escalations:
            print(f"  {key}: {hits}")
    return 0 if _count_keys(judge_out) >= len(rows) else 1


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--model", default="claudecode:claude-opus-5-5")
    p.add_argument("--judge-model", default="claudecode:claude-opus-5-5")
    p.add_argument("--n", type=int, default=15)
    p.add_argument("--pool", type=int, default=120, help="候補として走査するアイテム数")
    p.add_argument("--min-lines", type=int, default=2, help="線がこれ未満の窓は比べない")
    p.add_argument("--fresh", action="store_true", help="凍結窓を再構築する (既定は再利用)")
    p.add_argument("--dry", action="store_true", help="LLM を呼ばずプロンプトと窓数だけ確認する")
    p.add_argument("--judge", action="store_true")
    p.add_argument("--src", type=Path)
    p.add_argument("--judge-out", type=Path)
    a = p.parse_args()
    return asyncio.run(judge(a) if a.judge else generate(a))


if __name__ == "__main__":
    raise SystemExit(main())
