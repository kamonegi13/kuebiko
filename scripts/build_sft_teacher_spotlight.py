#!/usr/bin/env python3
"""spotlight の SFT 教師対を外部 LLM で生成する (N1.5 = N 族の task 拡張材料)。

本番 DB の Sonnet 出力 (174 件) は**プロンプトが保存されていない**ため流用できない。
代わりに本番コードパス (``generate_spotlight``) を過去日付の ``now=`` で駆動し、
組み上がったプロンプトを RecordingClient で捕獲して教師 (Opus) の出力と対で保存する
— プロンプトと出力が同時に生成されるので再構築の時代錯誤は生じない。

不変条件:
- **評価予約日 (直近 ``--eval-reserve-days`` 日) は収穫しない** (train/eval 分離。
  凍結評価は同じ再構築を予約日に対して行う)。
- **ローカル fallback 無効で実行** (``LLM_LOCAL_FALLBACK=0`` — 外部が落ちたとき
  ローカル出力が教師として混入するのが最悪の事故)。
- 材料不足で skip した (pir, date) も記録し、resume 時に再呼出しない。
- 隣接日は候補記事が重複しプロンプトが強く相関するため、日付は ``--stride`` 日おき。

使用例 (コンテナ内):
    docker exec -e LLM_LOCAL_FALLBACK=0 kuebiko python \\
        scripts/build_sft_teacher_spotlight.py --model claudecode:opus --days 36
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config_loader import load_app_config  # noqa: E402
from src.tools.llm_client import LLMClient  # noqa: E402
from src.tools.model_tiers import Step, build_llm_for_ref  # noqa: E402

DEFAULT_OUT = Path("data/mlx/teacher/spotlight.jsonl")
_T = TypeVar("_T", bound=BaseModel)

# 出力の最低品質 (これ未満は教師として保存しない)
_MIN_HEADLINE_CHARS = 40
_MIN_OUTLOOK_CHARS = 300


class RecordingClient:
    """直近の (prompt, 出力) を捕獲する透過ラッパ。委譲先は教師 client。"""

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner
        self.last: tuple[str, str] | None = None

    @property
    def model(self) -> str:
        return self._inner.model

    async def generate_structured(self, prompt: str, schema: type[_T], **kw: Any) -> _T:
        out = await self._inner.generate_structured(prompt, schema, **kw)
        dump = out.model_dump() if hasattr(out, "model_dump") else vars(out)
        self.last = (prompt, json.dumps(dump, ensure_ascii=False))
        return out


def _done_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {
        json.loads(line)["key"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


async def main_async(args: argparse.Namespace) -> int:
    from src.pir.integration import get_pir_config
    from src.spotlight.generator import generate_spotlight

    cfg = load_app_config()
    teacher = build_llm_for_ref(args.model, Step.PIR_SPOTLIGHT, cfg)
    rec = RecordingClient(teacher)

    pirs = [p for p in get_pir_config().priorities if p.enabled and p.spotlight.enabled]
    # 直近 eval_reserve_days は凍結評価用に予約 (収穫しない)
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    dates = [
        today - timedelta(days=d)
        for d in range(args.eval_reserve_days, args.days, args.stride)
    ]
    done = _done_keys(args.out)
    print(
        f"PIR {len(pirs)} × 日付 {len(dates)} "
        f"(予約 {args.eval_reserve_days} 日を除外) / 済 {len(done)}",
        file=sys.stderr,
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    ok = skipped = failed = rejected = 0
    consecutive = 0
    with args.out.open("a", encoding="utf-8") as fh:
        for date in dates:
            for pir in pirs:
                key = f"{pir.id}:{date.date().isoformat()}"
                if key in done:
                    continue
                rec.last = None
                try:
                    record = await generate_spotlight(
                        pir, llm=rec, period_type=args.period, now=date  # type: ignore[arg-type]
                    )
                except Exception as exc:  # noqa: BLE001 — 1 件の失敗で全体を落とさない
                    failed += 1
                    consecutive += 1
                    print(f"  {key} FAIL {type(exc).__name__}: {str(exc)[:80]}", flush=True)
                    if consecutive >= 5:
                        # 中断 = 失敗で返す (rc=0 だとリトライ層が完了と誤認する)
                        print("連続失敗が上限 — 中断 (rc=1)", file=sys.stderr)
                        return 1
                    continue
                consecutive = 0
                if record is None or rec.last is None:
                    skipped += 1  # 材料不足 — 記録して以後スキップ
                    fh.write(json.dumps({"key": key, "skipped": True}) + "\n")
                    fh.flush()
                    continue
                if (
                    len(record.headline) < _MIN_HEADLINE_CHARS
                    or len(record.outlook) < _MIN_OUTLOOK_CHARS
                ):
                    rejected += 1
                    print(f"  {key} 除外 (出力が短すぎる)", flush=True)
                    continue
                prompt, completion = rec.last
                fh.write(
                    json.dumps(
                        {"key": key, "prompt": prompt, "completion": completion},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                fh.flush()
                ok += 1
                if ok % 10 == 0:
                    print(f"  採用 {ok} / skip {skipped} / 失敗 {failed}", flush=True)

    print(f"\n完了: 採用 {ok} / skip {skipped} / 除外 {rejected} / 失敗 {failed} → {args.out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True, help="教師モデル ref (例 claudecode:opus)")
    ap.add_argument("--days", type=int, default=36, help="過去何日分の窓まで遡るか")
    ap.add_argument("--stride", type=int, default=3, help="日付の間隔 (隣接日の相関回避)")
    ap.add_argument("--eval-reserve-days", type=int, default=3, help="凍結評価用の予約日数")
    ap.add_argument("--period", default="rolling7", help="spotlight period_type")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
