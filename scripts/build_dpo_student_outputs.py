#!/usr/bin/env python3
"""教師プロンプトに対する学生モデル (N1/S1) の出力を生成する (N2/S2 = IPO/DPO の材料)。

preference 学習のペアは (chosen=教師 Opus, rejected=学生の実出力の乖離分) で組む。
蛇口 (event_draft_rejects) は 3 日で 26 件と供給が細いため、主材料はこちら:
学生自身の出力を rejected 側に使う = on-policy に近い preference データになる。

不変条件:
- **生成条件は本番同一** (schema 制約 / think=False / temperature 0.2 = 評価と同条件)。
- 教師ファイルの行順・article_id を保ち、resume 可能 (途中中断しても追記再開)。
- 失敗は失敗として記録し、教師出力や既定値で埋めない (build_sft_teacher_* と同じ規律)。

使用例 (ホスト、Ollama 直):
    uv run python scripts/build_dpo_student_outputs.py \\
        --teacher data/mlx/teacher/triage.jsonl --task triage \\
        --model kuebiko-sft:s1 --out data/mlx/dpo/student_s1_triage.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.tools.llm_client import OllamaClient  # noqa: E402

_MAX_CONSECUTIVE_FAILURES = 8


def _schema_for(task: str) -> type[Any]:
    if task == "triage":
        from src.tools.article_triage import TriageDecision

        return TriageDecision
    if task == "article_summary":
        from src.pipeline.summary import SummaryOutput

        return SummaryOutput
    if task == "event_news":
        from src.eventnews.models import EventNewsDraft

        return EventNewsDraft
    # S 第 2 陣 (N1.5/S1.5 学習後の on-policy 再生成用 — 明白マージン回避のため
    # 拡張 SFT 前のモデルでは呼ばないこと)
    if task == "pair_judge":
        from src.eventnews.pair_judge import PairVerdict

        return PairVerdict
    if task == "event_kind":
        from src.eventnews.event_kind import KindVerdict

        return KindVerdict
    if task == "pir_judge":
        from src.pir.llm_judge import JudgeVerdict

        return JudgeVerdict
    raise ValueError(f"未知の task: {task}")


def _load_rows(path: Path) -> list[dict[str, Any]]:
    rows = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        d = json.loads(line)
        if d.get("skipped"):
            continue  # 収穫時の材料不足マーカー (対にならない)
        d.setdefault("article_id", d.get("key", f"{path.stem}:{i}"))
        rows.append(d)
    return rows


def _done_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {
        json.loads(line)["article_id"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


async def main_async(args: argparse.Namespace) -> int:
    schema = _schema_for(args.task)
    rows = _load_rows(args.teacher)
    done = _done_ids(args.out)
    todo = [r for r in rows if r["article_id"] not in done]
    print(f"教師 {len(rows)} / 済 {len(done)} / 今回 {len(todo)}", file=sys.stderr)

    llm = OllamaClient(base_url=args.base_url, model=args.model, timeout_seconds=args.timeout)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    ok = failed = 0
    consecutive = 0
    with args.out.open("a", encoding="utf-8") as fh:
        for i, row in enumerate(todo, start=1):
            try:
                out = await llm.generate_structured(
                    row["prompt"],
                    schema=schema,
                    system=row.get("system"),
                    think=False,
                    temperature=0.2,
                )
            except Exception as exc:  # noqa: BLE001 — 1 件の失敗で全体を落とさない
                failed += 1
                consecutive += 1
                print(f"  {i}/{len(todo)} FAIL {type(exc).__name__}: {str(exc)[:80]}", flush=True)
                if consecutive >= _MAX_CONSECUTIVE_FAILURES:
                    print("連続失敗が上限 — 経路の恒久障害を疑い中断", file=sys.stderr)
                    break
                continue
            consecutive = 0
            fh.write(
                json.dumps(
                    {
                        "article_id": row["article_id"],
                        "student": json.dumps(out.model_dump(), ensure_ascii=False),
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
    ap.add_argument("--teacher", type=Path, required=True)
    ap.add_argument(
        "--task",
        required=True,
        choices=[
            "triage",
            "article_summary",
            "event_news",
            "pair_judge",
            "event_kind",
            "pir_judge",
        ],
    )
    ap.add_argument("--model", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--base-url", default="http://localhost:11434")
    ap.add_argument("--timeout", type=float, default=300.0)
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
