#!/usr/bin/env python3
"""状況総括の凍結審判セットを作る (CoT 蒸留の合否線、2026-09-15)。

過去窓の ``status_synthesis.tradecraft.grounded_estimate`` から **render プロンプトだけ**
を本番と同じ seam (``build_render_plan``) で再構築して凍結する。LLM は一切呼ばない。

なぜ凍結するか: 腕ごとに入力を作り直すと「同じものを測っている」保証が消える
(2026-09-08 の時代混在と同型の事故)。**全腕がこの 1 ファイルのプロンプトを読む**ことで
差分がモデルの差だけになる。

不変条件:
- 採った窓は教師収穫の対象外にする (``build_sft_teacher_synthesis.py --eval-reserve-days``
  に、このスクリプトが印字する日数以上を渡す)。重なると生徒は答えを見て答える。
- CoT 版 (``--cot``) と通常版でプロンプトが変わるため、審判セットも腕の形で分けて作る。

使用例 (コンテナ内。DB 読みのみ・GPU 不要):
    docker exec kuebiko python scripts/build_synthesis_judge_set.py --n 15
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.synthesis.grounded.estimate import Estimate, estimate_from_dict  # noqa: E402
from src.synthesis.grounded.render import build_render_plan  # noqa: E402

DEFAULT_OUT = Path("data/mlx/synthesis_judge_set.json")
#: 判定がこれ未満の窓は審判に使わない (静穏すぎて腕の差が出ない)。
_MIN_JUDGMENTS = 2


def _estimate_of(tradecraft: str) -> Estimate | None:
    """tradecraft JSON から grounded_estimate を取り出す (無い/壊れは None)。"""
    if not tradecraft:
        return None
    try:
        data = json.loads(tradecraft)
    except json.JSONDecodeError:
        return None
    raw = data.get("grounded_estimate") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return None
    try:
        return estimate_from_dict(raw)
    except (KeyError, TypeError, ValueError):
        return None


def _period_label(period_type: str, est: Estimate) -> str:
    """本番と同じ導出の期間ラベル (daily は「当日 00:00 JST 〜 period_end JST」)。"""
    from src.synthesis.generator import _resolve_period

    _s, _e, label, _lb, _bw = _resolve_period(period_type=period_type, now=est.period_end)
    return label


def build(args: argparse.Namespace) -> int:
    repo = RunHistoryRepository()
    records = repo.list_synthesis(period_type=args.period, limit=args.scan)
    items: list[dict[str, Any]] = []
    skipped_no_estimate = skipped_thin = 0
    for rec in records:
        est = _estimate_of(rec.tradecraft)
        if est is None:
            skipped_no_estimate += 1
            continue
        if len(est.judgments) < _MIN_JUDGMENTS:
            skipped_thin += 1
            continue
        plan = build_render_plan(
            est=est, period_label=_period_label(args.period, est), cot_notes=args.cot
        )
        items.append(
            {
                "key": f"synth:{args.period}:{est.period_start.date().isoformat()}",
                "period_type": args.period,
                "period_start": est.period_start.isoformat(),
                "headline_mode": plan.mode,
                "headline_judgment_id": plan.head.id if plan.head else "",
                "judgments": len(est.judgments),
                "prompt_chars": len(plan.prompt),
                "prompt": plan.prompt,
            }
        )
        if len(items) >= args.n:
            break

    if len(items) < args.n:
        print(
            f"⚠ 要求 {args.n} 件に対し {len(items)} 件しか集まらなかった "
            f"(estimate 無し {skipped_no_estimate} / 判定 {_MIN_JUDGMENTS} 件未満 {skipped_thin})",
            file=sys.stderr,
        )
        if not items:
            return 1

    modes = {
        m: sum(1 for i in items if i["headline_mode"] == m) for m in ("moved", "quiet", "plain")
    }
    oldest = min(i["period_start"] for i in items)
    reserve_days = (datetime.now(UTC) - datetime.fromisoformat(oldest)).days + 1
    payload = {
        "built_at": datetime.now(UTC).isoformat(),
        "period_type": args.period,
        "cot_notes": args.cot,
        "min_judgments": _MIN_JUDGMENTS,
        # 収穫側へ渡す予約日数。これ未満で収穫すると審判窓が教師に混じる。
        "required_eval_reserve_days": reserve_days,
        "headline_modes": modes,
        "items": items,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    chars = sorted(i["prompt_chars"] for i in items)
    print(f"凍結 {len(items)} 件 → {args.out}")
    print(f"  headline mode: {modes}")
    print(f"  prompt 文字数: 最小 {chars[0]} / 中央 {chars[len(chars) // 2]} / 最大 {chars[-1]}")
    print(f"  収穫の予約日数: --eval-reserve-days {reserve_days} 以上")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=15, help="凍結する窓の数")
    ap.add_argument("--scan", type=int, default=60, help="新しい順に走査する窓の数")
    ap.add_argument("--period", default="daily")
    ap.add_argument("--cot", action="store_true", help="analysis_notes 欄つきのプロンプトで凍結")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    return build(ap.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
