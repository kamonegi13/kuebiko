"""教師較正の実測 — E1 錨のある事例で「教師が本当に強いか」を正解基準で測る。

§10.2d は凍結 goldset (86 件) で教師と gemma を比較したが、goldset には E1 ラベルが
**1 件も含まれない** (実測 0 件) ため、測れたのは「一致率」であって正誤ではなかった。
本スクリプトは逆に **E1 ラベルのある記事だけ**を母集団にし、決定論の錨に対する
**正解率**を測る。不変条件 13 (パリティ・優越の主張はラベル有り場のみ) の実践であり、
教師ラベルを報酬源として許可してよい範囲 (§文献調査 報酬源 2 層目) を決める材料になる。

測定設計:
- 3 者比較 (不変条件 5): 本番 main を 2 回 (a/b) 走らせて**揺らぎの床**を作り、
  教師 1 回をそれと比べる。床を超えない差は「差」であって優越ではない。
- **候補未到達は別掲** (パネルと同じ規律): 正解が候補集合に無い事例は構造ゲートの
  問題であり、どのモデルでも選べない。到達率を誤り率に混ぜない。
- **few-shot は構造的 off** (§13-6): 測定器が自分の監査対象を教材に読む経路を断つ。

出力は data/eval/runs/<label>.jsonl (1 行 1 事例) + stdout のサマリ。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RUNS_DIR = Path("data/eval/runs")
DEFAULT_TEACHER = "claudecode:sonnet"
DEFAULT_LOOKBACK_DAYS = 400
# 本番ジョブ (毎時 triage 等) と Ollama を奪い合わないよう控えめに固定する。
DEFAULT_CONCURRENCY = 2


@dataclass(frozen=True)
class CaseResult:
    """1 事例に対する各判定者の答えと正誤。"""

    article_id: str
    truth: str
    production: str
    reachable: bool
    candidate_count: int
    verdicts: dict[str, str]

    def correct(self, judge: str) -> bool:
        return self.verdicts.get(judge, "") == self.truth


def _dedupe_by_article(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """articles は article_id 一意でない (join fan-out) ので本文最長の 1 行に正規化する。"""
    best: dict[str, dict[str, Any]] = {}
    for c in cases:
        prev = best.get(c["article_id"])
        if prev is None or len(c.get("body") or "") > len(prev.get("body") or ""):
            best[c["article_id"]] = c
    return sorted(best.values(), key=lambda c: c["article_id"])


async def _judge_one(llm: Any, case: dict[str, Any], candidates: list[Any], body: str) -> str:
    """1 判定者 1 事例。失敗は "(error)" (空文字 = モデルが空を返した、と区別する)。"""
    from src.cti.judgment_classifier import classify_judgment

    try:
        out = await classify_judgment(
            llm,
            title=case["title"],
            category=case["category"] or None,
            body=body,
            published=case["published"],
            candidates=candidates,
            allow_fewshot=False,  # §13-6: 測定経路は構造的に遮断
        )
    except Exception as e:  # noqa: BLE001 — 1 件の失敗で測定全体を止めない
        print(f"  ! {case['article_id']}: {type(e).__name__}: {e}", file=sys.stderr)
        return "(error)"
    if out is None:
        return "(error)"
    return str(out.subject_actor_id or "")


async def _run_case(
    case: dict[str, Any],
    judges: list[tuple[str, Any]],
    aliases: Any,
    sem: asyncio.Semaphore,
) -> CaseResult:
    from src.cti.judgment_classifier import _BODY_MAX_CHARS
    from src.pipeline.persistence import _relevant_actors

    body = (case["body"] or "")[:_BODY_MAX_CHARS]
    candidates = _relevant_actors(aliases.find_all(f"{case['title']}\n{body}"), case["category"])
    reachable = any(a.id == case["truth"] for a in candidates)
    verdicts: dict[str, str] = {}
    if reachable:
        async with sem:
            for name, llm in judges:
                verdicts[name] = await _judge_one(llm, case, candidates, body)
    return CaseResult(
        article_id=case["article_id"],
        truth=case["truth"],
        production=case["production"],
        reachable=reachable,
        candidate_count=len(candidates),
        verdicts=verdicts,
    )


def _build_judges(teacher_ref: str) -> list[tuple[str, Any]]:
    """本番 main を 2 回 (床) + 教師 1 回。

    ティアはシャドーパネルと同じ ``ARTICLE_SUMMARY`` — 本番の judgment 判定は
    per-article LLM (要約と同じ llm オブジェクト) が実行するため、これが本番相当。
    ティア解決・denylist 検証・推論系モデルの think 強制は既存 factory に任せる。
    """
    from src.tuning.shadow_panel import _default_llm_factory

    return [
        ("main-a", _default_llm_factory(None)),
        ("main-b", _default_llm_factory(None)),
        ("teacher", _default_llm_factory(teacher_ref)),
    ]


def _summarize(results: list[CaseResult], judge_names: list[str]) -> str:
    reach = [r for r in results if r.reachable]
    lines = [
        "=== 教師較正 (E1 錨つき母集団) ===",
        f"事例 {len(results)} 件 / 判定対象 {len(reach)} 件 "
        f"(候補未到達 {len(results) - len(reach)} 件は別掲 — 構造ゲートの問題)",
        "",
    ]
    if not reach:
        lines.append("判定対象ゼロ — 測定不能")
        return "\n".join(lines)

    lines.append(f"{'判定者':10} {'正解率':>8} {'空':>5} {'error':>6}")
    acc: dict[str, float] = {}
    for j in judge_names:
        hits = sum(1 for r in reach if r.correct(j))
        empty = sum(1 for r in reach if r.verdicts.get(j, "") == "")
        errs = sum(1 for r in reach if r.verdicts.get(j) == "(error)")
        acc[j] = 100.0 * hits / len(reach)
        lines.append(f"{j:10} {acc[j]:>7.1f}% {empty:>5} {errs:>6}")

    floor = abs(acc["main-a"] - acc["main-b"])
    delta = acc["teacher"] - (acc["main-a"] + acc["main-b"]) / 2
    lines += [
        "",
        f"揺らぎの床 (main-a vs main-b): {floor:.1f}pt",
        f"教師 - main 平均: {delta:+.1f}pt",
        (
            "→ 床を超えない = 教師の優越は観測されず (パリティ)"
            if abs(delta) <= max(floor, 1e-9)
            else ("→ 床超えの優越" if delta > 0 else "→ 床超えの劣位")
        ),
    ]
    disagree = [r for r in reach if r.verdicts.get("teacher") != r.verdicts.get("main-a")]
    t_only = [r for r in disagree if r.correct("teacher") and not r.correct("main-a")]
    m_only = [r for r in disagree if r.correct("main-a") and not r.correct("teacher")]
    both_wrong = [r for r in disagree if not r.correct("teacher") and not r.correct("main-a")]
    lines += [
        "",
        f"教師 ≠ main-a: {len(disagree)} 件 "
        f"(教師のみ正 {len(t_only)} / main のみ正 {len(m_only)} / 双方誤 {len(both_wrong)})",
    ]
    return "\n".join(lines)


async def _main_async(args: argparse.Namespace) -> int:
    from src.cti.actor_normalizer import load_actor_aliases
    from src.storage.run_history import RunHistoryRepository

    repo = RunHistoryRepository()
    since = (datetime.now(UTC) - timedelta(days=args.lookback_days)).isoformat()
    cases = _dedupe_by_article(repo.fetch_subject_panel_cases(since))
    if args.limit:
        cases = cases[: args.limit]
    print(f"E1 事例 {len(cases)} 件 (重複正規化後) / 教師 = {args.teacher}")

    judges = _build_judges(args.teacher)
    judge_names = [n for n, _ in judges]
    aliases = load_actor_aliases()
    sem = asyncio.Semaphore(args.concurrency)

    results = await asyncio.gather(*[_run_case(c, judges, aliases, sem) for c in cases])

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RUNS_DIR / f"{args.label}.jsonl"
    with out_path.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(
                json.dumps(
                    {
                        "article_id": r.article_id,
                        "truth": r.truth,
                        "production": r.production,
                        "reachable": r.reachable,
                        "candidate_count": r.candidate_count,
                        "verdicts": r.verdicts,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    print(f"\n出力: {out_path}\n")
    print(_summarize(list(results), judge_names))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--label", default="teacher-e1", help="出力ラベル (data/eval/runs/<label>.jsonl)"
    )
    p.add_argument("--teacher", default=DEFAULT_TEACHER, help="教師モデル参照")
    p.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    p.add_argument("--limit", type=int, default=0, help="先頭 N 件に絞る (0=全件)")
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    args = p.parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
