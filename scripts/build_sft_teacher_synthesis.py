#!/usr/bin/env python3
"""synthesis narrative の SFT 教師対を外部 LLM で生成する (N1.5 の材料)。

本番コードパス (``generate_synthesis``) を過去日付の ``now=`` で駆動し、narrative 段の
プロンプトを RecordingClient で捕獲して教師の出力と対で保存する。プロンプトと出力は
同時に生成されるため再構築の時代錯誤は生じない (spotlight 収穫と同じ方式)。

コスト設計: 上流 (detect / ACH) は**ローカル**で回す — 教師出力として保存するのは
narrative 段だけであり、足場の外部消費は無駄。プロンプトに載る台帳判定の質が本番
(reasoning=外部) と微差になる点は許容 (docstring 明記の設計判断)。

不変条件:
- 直近 ``--eval-reserve-days`` 日は凍結評価用に予約 (収穫しない)。
- ``LLM_LOCAL_FALLBACK=0`` で実行 (ローカル出力の教師混入防止)。
- narrative 段が複数回呼ばれる実装変更に備え、捕獲は list (全呼出を保存)。

使用例 (コンテナ内・GPU 静穏時):
    docker exec -e LLM_LOCAL_FALLBACK=0 kuebiko python \\
        scripts/build_sft_teacher_synthesis.py --model claudecode:opus --days 80
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
from src.tools.llm_client import LLMClient, OllamaClient  # noqa: E402
from src.tools.model_tiers import Step, build_llm_for_ref  # noqa: E402

DEFAULT_OUT = Path("data/mlx/teacher/synthesis.jsonl")
_T = TypeVar("_T", bound=BaseModel)
_MIN_COMPLETION_CHARS = 400  # これ未満の narrative は教師として保存しない


class RecordingClient:
    """narrative 段の全 (prompt, 出力) を捕獲する透過ラッパ。"""

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner
        self.captured: list[tuple[str, str]] = []

    @property
    def model(self) -> str:
        return self._inner.model

    async def generate_structured(self, prompt: str, schema: type[_T], **kw: Any) -> _T:
        out = await self._inner.generate_structured(prompt, schema, **kw)
        dump = out.model_dump() if hasattr(out, "model_dump") else vars(out)
        self.captured.append((prompt, json.dumps(dump, ensure_ascii=False)))
        return out

    async def generate(self, prompt: str, **kw: Any) -> Any:
        resp = await self._inner.generate(prompt, **kw)
        self.captured.append((prompt, resp.text))
        return resp


def _done_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {
        json.loads(line)["key"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


async def main_async(args: argparse.Namespace) -> int:
    from src.synthesis.generator import generate_synthesis

    cfg = load_app_config()
    teacher = build_llm_for_ref(args.model, Step.SYNTHESIS_NARRATIVE, cfg)
    # 足場はローカル (外部消費の節約。本番は reasoning=外部でも、保存対象は narrative のみ)
    fast_llm = OllamaClient(
        base_url=cfg.ollama_base_url, model="gemma4:26b", timeout_seconds=900.0
    )
    analysis_llm = OllamaClient(
        base_url=cfg.ollama_base_url, model="gemma4:31b", timeout_seconds=900.0
    )

    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    dates = [
        today - timedelta(days=d)
        for d in range(args.eval_reserve_days, args.days, args.stride)
    ]
    done = _done_keys(args.out)
    print(f"日付 {len(dates)} / 済 {len(done)}", file=sys.stderr)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    ok = failed = rejected = 0
    with args.out.open("a", encoding="utf-8") as fh:
        for date in dates:
            base_key = f"synth:{args.period}:{date.date().isoformat()}"
            if any(k.startswith(base_key) for k in done):
                continue
            rec = RecordingClient(teacher)
            try:
                res = await generate_synthesis(
                    llm=rec,  # type: ignore[arg-type]  # LLMClient 互換の透過ラッパ
                    period_type=args.period,
                    now=date,
                    fast_llm=fast_llm,
                    analysis_llm=analysis_llm,
                )
            except Exception as exc:  # noqa: BLE001 — 1 窓の失敗で全体を落とさない
                failed += 1
                print(f"  {base_key} FAIL {type(exc).__name__}: {str(exc)[:80]}", flush=True)
                continue
            if res.record is None or not rec.captured:
                rejected += 1
                print(f"  {base_key} 生成なし ({res.error})", flush=True)
                continue
            for i, (prompt, completion) in enumerate(rec.captured):
                if len(completion) < _MIN_COMPLETION_CHARS:
                    continue
                fh.write(
                    json.dumps(
                        {"key": f"{base_key}:{i}", "prompt": prompt, "completion": completion},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            fh.flush()
            ok += 1
            print(f"  {base_key} 採用 (捕獲 {len(rec.captured)} 呼出)", flush=True)

    print(f"\n完了: 窓 {ok} / 失敗 {failed} / 生成なし {rejected} → {args.out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True, help="教師モデル ref (例 claudecode:opus)")
    ap.add_argument("--days", type=int, default=80)
    ap.add_argument("--stride", type=int, default=2)
    ap.add_argument("--eval-reserve-days", type=int, default=3)
    ap.add_argument("--period", default="daily")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
