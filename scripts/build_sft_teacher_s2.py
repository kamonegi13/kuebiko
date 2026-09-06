#!/usr/bin/env python3
"""S 第 2 陣 (pair_judge / event_kind / pir_judge) の SFT 教師対を外部 LLM で生成する。

S1 (triage + article_summary) の拡張材料。プロンプトは**本番のビルダーをそのまま借り**、
completion は本番 schema の JSON (PairVerdict / KindVerdict / JudgeVerdict)。

train/eval 汚染の回避 (task ごとに方式が違う):
- pair_judge: ラベル 365 対は features ファイルに索引でしか残っておらず対 id の復元が
  不確実 → **時間分離** (ラベル作成期 2026-09-01 以前の shadow 行は収穫しない)。
- event_kind: data/eval/event_kind.json の 455 id を直接除外。
- pir_judge: 凍結評価が無い task → 直近 ``--eval-reserve-days`` 日を評価用に予約。

dedup_judge は対象外 (本番の候補対がどこにも記録されておらず、忠実な入力サンプリングが
できない。候補の shadow 記録を先に敷く = 第 3 陣)。

使用例 (コンテナ内、fallback 無効必須):
    docker exec -e LLM_LOCAL_FALLBACK=0 kuebiko python \\
        scripts/build_sft_teacher_s2.py --task event_kind --model claudecode:opus --limit 400
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config_loader import load_app_config  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.tools.model_tiers import Step, build_llm_for_ref  # noqa: E402

_MAX_CONSECUTIVE_FAILURES = 8
_PAIR_LABEL_ERA_END = "2026-09-01"  # これ以前の shadow 行はラベル母集団 → 収穫しない

_EVENT_KIND_LABELS = Path("data/eval/event_kind.json")


def _rows(sql: str, params: tuple[Any, ...] = ()) -> list[list[Any]]:
    repo = RunHistoryRepository()
    with repo._connect() as conn:  # noqa: SLF001 — 読み取り専用
        out = []
        for r in conn.execute(sql, params).fetchall():
            out.append(list(r.values()) if hasattr(r, "values") else list(r))
        return out


def _build_pair_judge(limit: int) -> list[dict[str, Any]]:
    """event_pair_shadow のラベル期以降の対 → (key, prompt, system, schema名)。"""
    from src.eventnews.pair_judge import SYSTEM, build_prompt

    # ⚠ 同名列は AS 別名必須 — PG backend は行を dict で返すため la.title と ra.title が
    # 1 キーに潰れる (requeue_mojibake_bodies と同じ罠)。
    rows = _rows(
        "SELECT s.id AS sid, la.title AS lt, la.summary AS ls, la.feed_title AS lf, "
        "ra.title AS rt, ra.summary AS rs, ra.feed_title AS rf "
        "FROM event_pair_shadow s "
        "JOIN articles la ON la.article_id = s.left_id "
        "JOIN articles ra ON ra.article_id = s.right_id "
        "WHERE s.observed_at > ? "
        "GROUP BY s.id, la.title, la.summary, la.feed_title, ra.title, ra.summary, ra.feed_title "
        "ORDER BY s.id DESC LIMIT ?",
        (_PAIR_LABEL_ERA_END, limit),
    )
    return [
        {
            "key": f"pair:{r[0]}",
            "prompt": build_prompt(
                str(r[1] or ""),
                str(r[2] or ""),
                str(r[3] or ""),
                str(r[4] or ""),
                str(r[5] or ""),
                str(r[6] or ""),
            ),
            "system": SYSTEM,
        }
        for r in rows
    ]


def _build_event_kind(limit: int) -> list[dict[str, Any]]:
    from src.eventnews.event_kind import KINDS, SYSTEM

    labeled: set[str] = (
        set(json.loads(_EVENT_KIND_LABELS.read_text())) if _EVENT_KIND_LABELS.exists() else set()
    )
    rows = _rows(
        "SELECT article_id, title, summary FROM articles "
        "WHERE title IS NOT NULL AND title <> '' AND summary IS NOT NULL AND summary <> '' "
        "AND created_at > NOW() - INTERVAL '60 days' "
        "GROUP BY article_id, title, summary ORDER BY article_id LIMIT ?",
        (limit * 3,),
    )
    out = []
    for aid, title, summary in rows:
        if str(aid) in labeled:
            continue
        # 本番 classify() と同一のプロンプト組み立て (event_kind.py:69)
        prompt = (
            f"見出し: {title}\n要約: {(str(summary) or '')[:400]}\n\n"
            f"種別を 1 つ選んでください: {', '.join(KINDS)}"
        )
        out.append({"key": f"kind:{aid}", "prompt": prompt, "system": SYSTEM})
        if len(out) >= limit:
            break
    return out


def _build_pir_judge(limit: int, eval_reserve_days: int) -> list[dict[str, Any]]:
    """本番の対象選抜を state 空で借りる = 過去窓の候補ゲート通過分を全て再現。

    - ``state={}`` により「判定済み」除外を無効化 (教師は独立に判定し直す)。
    - 直近 ``eval_reserve_days`` 日は凍結評価用に予約 (収穫しない)。
    - PIR 間はラウンドロビン (本番 judge_pending と同じ公平配分)。
    """
    from itertools import zip_longest

    from src.pir import evaluator as pe
    from src.pir import llm_judge as lj
    from src.pir.integration import get_pir_config

    repo = RunHistoryRepository()
    rows, actor_map = pe._load_posted_rows(  # noqa: SLF001 — 本番と同一の選抜を共有
        repo=repo, lookback_hours=45 * 24, limit=lj._ROW_LIMIT
    )
    cutoff = (datetime.now(UTC) - timedelta(days=eval_reserve_days)).isoformat()

    per_pir: list[list[dict[str, Any]]] = []
    pirs = [p for p in get_pir_config().enabled_priorities() if p.llm_judge.enabled and p.match]
    for pir in pirs:
        _, targets = lj._judge_targets_for_pir(pir, rows, {}, actor_map)  # noqa: SLF001
        items = []
        for row in targets:
            if str(row["created_at"] or "") > cutoff:
                continue  # 凍結評価用に予約
            items.append(
                {
                    "key": f"pirj:{pir.id}:{row['article_id']}",
                    "prompt": lj.build_judge_prompt(pir, row),
                    "system": None,
                }
            )
        per_pir.append(items)
    out = [
        item for round_items in zip_longest(*per_pir) for item in round_items if item is not None
    ]
    return out[:limit]


def _schema_for(task: str) -> type[Any]:
    if task == "pair_judge":
        from src.eventnews.pair_judge import PairVerdict

        return PairVerdict
    if task == "event_kind":
        from src.eventnews.event_kind import KindVerdict

        return KindVerdict
    from src.pir.llm_judge import JudgeVerdict

    return JudgeVerdict


def _done_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {
        json.loads(line)["key"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


async def main_async(args: argparse.Namespace) -> int:
    if args.task == "pair_judge":
        items = _build_pair_judge(args.limit)
    elif args.task == "event_kind":
        items = _build_event_kind(args.limit)
    else:
        items = _build_pir_judge(args.limit, args.eval_reserve_days)
    schema = _schema_for(args.task)

    done = _done_keys(args.out)
    todo = [x for x in items if x["key"] not in done]
    print(f"候補 {len(items)} / 済 {len(done)} / 今回 {len(todo)}", file=sys.stderr)

    llm = build_llm_for_ref(args.model, Step.TRIAGE, load_app_config())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    ok = failed = 0
    consecutive = 0
    with args.out.open("a", encoding="utf-8") as fh:
        for i, item in enumerate(todo, start=1):
            try:
                out = await llm.generate_structured(
                    item["prompt"],
                    schema=schema,
                    system=item["system"],
                    think=False,
                    max_tokens=400,
                )
            except Exception as exc:  # noqa: BLE001
                failed += 1
                consecutive += 1
                print(f"  {i}/{len(todo)} FAIL {type(exc).__name__}: {str(exc)[:80]}", flush=True)
                if consecutive >= _MAX_CONSECUTIVE_FAILURES:
                    # ⚠ rc=0 で返すとリトライ層が「完了」と誤認する (2026-09-06 実害:
                    # bridge 停止中に 3 課題が 0 行のまま素通りした)。中断 = 失敗。
                    print("連続失敗が上限 — 中断 (rc=1)", file=sys.stderr)
                    return 1
                continue
            consecutive = 0
            fh.write(
                json.dumps(
                    {
                        "key": item["key"],
                        "prompt": item["prompt"],
                        "system": item["system"],
                        "completion": json.dumps(out.model_dump(), ensure_ascii=False),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            fh.flush()
            ok += 1
            if i % 50 == 0:
                print(f"  {i}/{len(todo)} ok={ok} 失敗={failed}", flush=True)
    print(f"\n完了: 採用 {ok} / 失敗 {failed} → {args.out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--task", required=True, choices=["pair_judge", "event_kind", "pir_judge"])
    ap.add_argument("--model", required=True)
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--eval-reserve-days", type=int, default=3)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    if args.out is None:
        args.out = Path(f"data/mlx/teacher/{args.task}.jsonl")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
