#!/usr/bin/env python3
"""GraphRAG の効果を状況総括 (synthesis narrative) で測る — 学習なし・Opus の対読 (2026-10-08)。

Spotlight では線でたどった候補外の関連事象が 12:3 で効いた (graphrag_spotlight_compare.py)。
次は状況総括 (render 段 — 台帳の estimate を射影する唯一の LLM 呼出) で同じ問いを測る
(docs/research/llm_training/next_models_s22_n20.md §8.2)。

**本番と同じ入力の形**: render は ``src/synthesis/grounded/render.py:build_render_plan`` が
唯一の seam (保存済み estimate から LLM 呼出なしでプロンプトを再構築できる、
``build_sft_teacher_synthesis.py`` と同じ方式)。``generate_synthesis(now=過去日付)`` の再生は
禁止 (CLAUDE.md §7 2026-09-15: 過去 now での再生が台帳へ過去時刻で書き込む事故が実際に起きた)。
本スクリプトは ``generate_synthesis`` を import しない。

- 腕 A: 保存済み estimate から再構築した本番 render プロンプト
- 腕 B: 腕 A + 「線でたどった関連事象」の節 (【判定間の関係】の直後、本文指示の直前)。
  線は judgment の証拠記事 (``KeyJudgment.evidence[*].article_id``、estimate に既に埋め込み済み
  — 追加の DB 読みは event_item_members 以降だけ) が属する事象から、本番の関係表示と同じ導出
  (``src.eventnews.relations.relations_by_event``) でたどる。期間終了より後に報じられた事象は除く
- 書き手はどちらも既定 Opus (学習なし)。審判は盲検の対読 (Verdict は spotlight 版を再利用)

凍結窓は初回に ``data/eval/graphrag_synthesis_frozen.jsonl`` へ保存し、以後は再構築しない
(再実行で同じ入力)。

    docker exec kuebiko python scripts/graphrag_synthesis_compare.py --dry --n 15
    docker exec kuebiko python scripts/graphrag_synthesis_compare.py --n 15
    docker exec kuebiko python scripts/graphrag_synthesis_compare.py --judge
出力: data/eval/graphrag_synthesis_frozen.jsonl (凍結入力) /
      data/eval/graphrag_synthesis.jsonl (生成、再開可能) /
      data/eval/graphrag_synthesis_judge.jsonl (対読、再開可能)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

from build_sft_teacher_synthesis import _estimate_of  # noqa: E402 — estimate 復元を再利用
from graphrag_spotlight_compare import Verdict, _arm  # noqa: E402 — Verdict/盲検戻しを再利用

from src.config_loader import load_app_config  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.synthesis.generator import _resolve_period  # noqa: E402
from src.synthesis.grounded.estimate import Estimate, KeyJudgment  # noqa: E402
from src.synthesis.grounded.render import (  # noqa: E402  # noqa: E402
    _MAX_TOKENS_BY_PERIOD,
    _TEMPERATURE,
    _WireSections,
    build_render_plan,
)
from src.tools.llm_client import LLMClient  # noqa: E402
from src.tools.model_tiers import Step, build_llm_for_ref  # noqa: E402

D = Path("/app/data/eval") if Path("/app/data/eval").exists() else _ROOT / "data" / "eval"
FROZEN = D / "graphrag_synthesis_frozen.jsonl"
OUT = D / "graphrag_synthesis.jsonl"
JUDGE_OUT = D / "graphrag_synthesis_judge.jsonl"

_REL_DAYS = 120
_MAX_LINES = 20
_MIN_JUDGMENTS = 2
_SEED = 1008
_INSERT_MARKER = "次の各セクションを日本語で書いてください"
_REL_LABELS = {
    "follow_up": "続報",
    "incident": "同じ出来事の関連",
    "side": "同じ出来事の別の側面",
    "campaign": "同一キャンペーン",
    "contains": "包含 (まとめ記事)",
    "same_actor": "同じアクター",
}
_BASIS = {"victim": "被害組織", "cve": "CVE", "cap": "道具", "actor": "攻撃者"}
_NOTE = (
    "上の線は、判定の証拠記事が属する事象が共有する指標 (被害組織・CVE・道具・攻撃者) から"
    "機械的に導いたものである。書くときは線を根拠として引用してよいが、線に無い判定どうし・"
    "事象どうしの関係は推測しない。"
)
# 2026-10-08 src/graph/render.py の実測知見: 節に乗る線の 97% が「同じアクター」で、中身は
# 攻撃者名の共有だけなのに格上げ (同一キャンペーン扱い) を誘発した。本スクリプトは別実装
# (judgment 単位で参照するため src/graph を直接使えない) だが、同じ安全策 (凡例 1 行 + 1 アク
# ターあたりの上限) を独立して入れる。
_SAME_ACTOR_LEGEND = (
    "「同じアクター」の線は攻撃者名の共有だけを示す。同一キャンペーン・同一の侵入経路の"
    "根拠にはならない。"
)
#: 1 アクターあたり、節全体で「同じアクター」の線を何本まで残すか (ハブ抑制)。
_SAME_ACTOR_HUB_CAP = 3
#: 線つき版で機械検出する「格上げ」語彙 (線が無ければ言えない接続の主張)。
ESCALATION_PHRASES: tuple[str, ...] = (
    "同一キャンペーン",
    "同じアクターによる",
    "一連の攻撃",
    "連携した",
    "共謀",
    "同じ侵入経路",
    "関連する作戦",
)


# ---------- 線の節 (GraphRAG の取得。後で src/graph/ への差し替え可能な 1 関数) ----------


def _basis_label(basis: str, canonical: Any) -> str:
    kind, _, value = basis.partition(":")
    if kind == "actor":
        actor = canonical(value)
        value = actor.canonical if actor is not None else value
    return f"{_BASIS.get(kind, kind)}: {value}"


def graph_block(
    repo: Any,
    rels: dict[str, list[Any]],
    first: dict[str, datetime],
    judgments: tuple[KeyJudgment, ...],
) -> str:
    """judgment の証拠記事が属する事象から線をたどった **候補外** の関連事象 (決定論)。

    参照は記事番号でなく判定の claim (synthesis render プロンプトは記事を numbered list で
    提示しないため)。線が無ければ空文字。
    """
    from src.cti.actor_normalizer import load_actor_aliases

    article_to_judgment: dict[str, KeyJudgment] = {
        e.article_id: j for j in judgments for e in j.evidence
    }
    article_ids = list(article_to_judgment)
    if not article_ids:
        return ""
    marks = ",".join("?" for _ in article_ids)
    with repo._connect() as conn:  # noqa: SLF001 — 読み取りのみ
        mem = conn.execute(
            "SELECT m.article_id, m.item_id FROM event_item_members m JOIN event_items i "
            f"ON i.id = m.item_id WHERE m.article_id IN ({marks}) AND i.merged_into IS NULL",
            tuple(article_ids),
        ).fetchall()
    ev_claim: dict[str, str] = {}
    for aid, ev in mem:
        j = article_to_judgment.get(str(aid))
        if j is not None:
            ev_claim.setdefault(str(ev), j.claim)
    if not ev_claim:
        return ""
    aliases = load_actor_aliases()
    seen: set[tuple[str, str, str]] = set()
    found: list[tuple[int, datetime, str, frozenset[str]]] = []
    for ev, claim in ev_claim.items():
        for r in rels.get(ev, []):
            other = r.b if r.a == ev else r.a
            # ``first`` は呼び手が窓終了前に絞り込み済み (未来の事象は None)。
            f = first.get(other)
            if other == ev or other in ev_claim or f is None:
                continue
            key = (min(ev, other), max(ev, other), r.rel_type)
            if key in seen:
                continue
            seen.add(key)
            head = repo.latest_event_versions([other]).get(other)
            if head is None or not head.headline:
                continue
            basis = "、".join(_basis_label(b, aliases.by_id) for b in r.basis) or "要約の類似"
            label = _REL_LABELS.get(r.rel_type, r.rel_type)
            line = (
                f"- 判定「{claim[:40]}」↔ 候補外の事象「{head.headline}」"
                f"({f.date().isoformat()}): {label} (共有: {basis})"
            )
            rank = 1 if r.rel_type == "same_actor" else 0
            actor_ids = frozenset(b.partition(":")[2] for b in r.basis if b.startswith("actor:"))
            found.append((rank, f, line, actor_ids))
    if not found:
        return ""
    found.sort(key=lambda t: (t[0], -t[1].timestamp()))
    # ハブ抑制: 「同じアクター」はアクターごとに節全体で _SAME_ACTOR_HUB_CAP 本まで
    # (多産なアクターが節を独占し、他の線を押し出すのを防ぐ)。
    kept: list[tuple[int, datetime, str, frozenset[str]]] = []
    actor_count: dict[str, int] = {}
    for item in found:
        rank, _f, _line, actor_ids = item
        if rank == 1 and any(actor_count.get(a, 0) >= _SAME_ACTOR_HUB_CAP for a in actor_ids):
            continue
        for a in actor_ids:
            actor_count[a] = actor_count.get(a, 0) + 1
        kept.append(item)
    lines = [t[2] for t in kept[:_MAX_LINES]]
    legend = f"{_SAME_ACTOR_LEGEND}\n" if any(t[0] == 1 for t in kept[:_MAX_LINES]) else ""
    return (
        f"【線でたどった関連事象】({len(lines)} 本 / 全 {len(found)} 本)\n"
        + "\n".join(lines)
        + f"\n{legend}{_NOTE}\n\n"
    )


def with_block(prompt: str, block: str) -> str:
    """本文指示 (``_INSERT_MARKER``) の直前に線の節を差し込む。"""
    at = prompt.find(_INSERT_MARKER)
    if at < 0:
        return prompt + "\n\n" + block
    return prompt[:at] + block + "\n" + prompt[at:]


# ---------- 窓の選定 (凍結。初回のみ構築し、以後は再構築しない) ----------


def _window_key(period_type: str, est: Estimate) -> str:
    return f"synth:{period_type}:{est.period_start.date().isoformat()}"


def _build_frozen(args: argparse.Namespace) -> list[dict[str, Any]]:
    from src.eventnews.relations import relations_by_event

    repo = RunHistoryRepository()
    rels = relations_by_event(repo, days=_REL_DAYS)
    with repo._connect() as conn:  # noqa: SLF001
        first = {
            str(r[0]): datetime.fromisoformat(str(r[1]).replace("Z", "+00:00"))
            for r in conn.execute("SELECT id, first_reported_at FROM event_items").fetchall()
        }
    records = repo.list_synthesis(period_type=args.period, limit=args.scan)
    windows: list[dict[str, Any]] = []
    no_lines = 0
    for rec in records:
        if len(windows) >= args.n:
            break
        est = _estimate_of(rec)
        if est is None or len(est.judgments) < _MIN_JUDGMENTS:
            continue
        # 窓の終了より後に報じられた事象は線から除く (未来の混入防止)。
        first_scoped = {k: v for k, v in first.items() if v < est.period_end}
        block = graph_block(repo, rels, first_scoped, est.judgments)
        n_lines = block.count("\n- ")
        if n_lines < args.min_lines:
            no_lines += 1
            continue
        _s, _e, label, _lb, _bw = _resolve_period(period_type=args.period, now=est.period_end)
        plan = build_render_plan(est=est, period_label=label, cot_notes=False)
        windows.append(
            {
                "key": _window_key(args.period, est),
                "prompt_a": plan.prompt,
                "prompt_b": with_block(plan.prompt, block),
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
    llm = build_llm_for_ref(
        args.model, Step.SYNTHESIS_NARRATIVE, load_app_config(), timeout_seconds=900.0
    )
    max_tokens = _MAX_TOKENS_BY_PERIOD.get(args.period, 6_000)
    sem = asyncio.Semaphore(2)

    async def arm(prompt: str) -> str | None:
        async with sem:
            try:
                out = await llm.generate_structured(
                    prompt=prompt,
                    schema=_WireSections,
                    temperature=_TEMPERATURE,
                    max_tokens=max_tokens,
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

JUDGE = """あなたは CTI 部門の上級分析官です。同じ台帳 (estimate) から射影された 2 つの
状況総括 (synthesis) を比べてください。参考として、判定間の関係と、判定の証拠記事が属する
事象から機械的に導いた線 (候補外の関連事象) を示します。線は一方の書き手にしか渡されていません。

次の観点で、読み手 (日本の CTI 担当者) の判断に役立つのはどちらかを決めてください。
1. 判定どうし・事象どうしのつながり (連鎖・継続性) を正しく捉え、個別の判定の言い換えを
   超えた全体像を示しているか
2. estimate・線に根拠の無い主張・関係の推測・確度の格上げが無いか (これがある方は大きく減点)
3. 見通し・留保・分からないことが具体的か

各観点で優れた方 (X / Y / 同等) を選び、最後に総合の勝者を決め、理由を 2-3 文で書く。
勝敗の理由には、出力の該当箇所を具体的に挙げること。

## estimate の判定一覧と線 (参考)
{context}

## 出力 X
{x}

## 出力 Y
{y}
"""


#: 格上げ語の直後 (この字数以内) にあれば否定・留保とみなす語。2026-10-08 の対読で、
#: 線つき版の「同一キャンペーン」は全件が「…の根拠にはならない」の否定だった
_NEGATION_WINDOW = 40
_NEGATIONS: tuple[str, ...] = (
    "根拠にはなら",
    "根拠とはなら",
    "根拠ではな",
    "根拠はな",
    "示すものではな",
    "意味するものではな",
    "を示す根拠",
    "かどうか",
    "か否か",
    "予断せ",
    "扱わな",
    "不明",
    "断定できな",
)


def _asserted(phrase: str, text: str) -> bool:
    """``phrase`` が否定・留保を伴わずに 1 回でも使われているか。"""
    start = 0
    while (i := text.find(phrase, start)) >= 0:
        tail = text[i + len(phrase) : i + len(phrase) + _NEGATION_WINDOW]
        if not any(n in tail for n in _NEGATIONS):
            return True
        start = i + len(phrase)
    return False


def _escalation_hits(a_text: str, b_text: str) -> list[str]:
    """線つき版 (b) にだけ、否定・留保なしで出た格上げ語彙 (機械検出)。"""
    return [p for p in ESCALATION_PHRASES if _asserted(p, b_text) and not _asserted(p, a_text)]


async def judge(args: argparse.Namespace) -> int:
    import random

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
            marker = "【変化した判定】"
            at = r["prompt_b"].find(marker)
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
        print(f"\n線つき版にのみ出た格上げ語彙: {len(escalations)}/{len(rows)} 窓")
        for key, hits in escalations:
            print(f"  {key}: {hits}")
    return 0 if _count_keys(judge_out) >= len(rows) else 1


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--model", default="claudecode:claude-opus-5-5")
    p.add_argument("--judge-model", default="claudecode:claude-opus-5-5")
    p.add_argument("--period", default="daily")
    p.add_argument("--n", type=int, default=15)
    p.add_argument("--scan", type=int, default=200, help="走査する保存記録の数")
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
