#!/usr/bin/env python3
"""synthesis narrative の SFT 教師対を外部 LLM で生成する (N1.5 の材料)。

本番コードパス (``generate_synthesis``) を過去日付の ``now=`` で駆動し、narrative 段の
プロンプトを RecordingClient で捕獲して教師の出力と対で保存する。プロンプトと出力は
同時に生成されるため再構築の時代錯誤は生じない (spotlight 収穫と同じ方式)。

⚠ **grounded モード (本番既定) では narrative ティアの ``llm`` は一切呼ばれない**
(2026-09-06 実測で判明)。``pipeline.build_estimate`` も ``render_record`` も
``ach_llm = analysis_llm`` を使う。よって教師は **analysis_llm 側**に挿す必要がある。

コスト設計: 1 窓あたり ACH 系が 7-8 呼出あるため全部を外部に投げると高い。本スクリプトは
**プロンプトが render テンプレート由来のときだけ教師 (外部) へ、それ以外はローカルへ**
振り分ける選択的ラッパを使う (捕獲対象 = 状況総括の散文 1 呼出/窓)。

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


#: render.j2 由来のプロンプトだけを教師へ回すための識別文字列 (テンプレート冒頭の固定句)。
#: 変わると**黙って 0 件になる**ので、実行時に 1 件も捕獲できなければ失敗として返す。
_RENDER_MARKER = "これは射影であって再分析ではない"


class SelectiveTeacherClient:
    """render 段だけ教師 (外部) へ、他はローカルへ振り分けて捕獲する。"""

    def __init__(self, teacher: LLMClient, local: LLMClient) -> None:
        self._teacher = teacher
        self._local = local
        self.captured: list[tuple[str, str]] = []

    @property
    def model(self) -> str:
        return self._teacher.model

    def _pick(self, prompt: str) -> tuple[LLMClient, bool]:
        is_render = _RENDER_MARKER in prompt
        return (self._teacher if is_render else self._local), is_render

    async def generate_structured(self, prompt: str, schema: type[_T], **kw: Any) -> _T:
        client, is_render = self._pick(prompt)
        out = await client.generate_structured(prompt, schema, **kw)
        if is_render:
            dump = out.model_dump() if hasattr(out, "model_dump") else vars(out)
            self.captured.append((prompt, json.dumps(dump, ensure_ascii=False)))
        return out

    async def generate(self, prompt: str, **kw: Any) -> Any:
        client, is_render = self._pick(prompt)
        resp = await client.generate(prompt, **kw)
        if is_render:
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
    fast_llm = OllamaClient(base_url=cfg.ollama_base_url, model="gemma4:26b", timeout_seconds=900.0)
    local_analysis = OllamaClient(
        base_url=cfg.ollama_base_url, model="gemma4:31b", timeout_seconds=900.0
    )

    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    dates = [
        today - timedelta(days=d) for d in range(args.eval_reserve_days, args.days, args.stride)
    ]
    done = _done_keys(args.out)
    print(f"日付 {len(dates)} / 済 {len(done)}", file=sys.stderr)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    ok = failed = rejected = 0
    consecutive = 0
    with args.out.open("a", encoding="utf-8") as fh:
        for date in dates:
            base_key = f"synth:{args.period}:{date.date().isoformat()}"
            if any(k.startswith(base_key) for k in done):
                continue
            rec = SelectiveTeacherClient(teacher, local_analysis)
            try:
                res = await generate_synthesis(
                    # ⚠ grounded では narrative 側は使われない。教師は analysis に挿す
                    llm=local_analysis,
                    period_type=args.period,
                    now=date,
                    fast_llm=fast_llm,
                    analysis_llm=rec,  # type: ignore[arg-type]  # LLMClient 互換ラッパ
                )
            except Exception as exc:  # noqa: BLE001 — 1 窓の失敗で全体を落とさない
                failed += 1
                consecutive += 1
                print(f"  {base_key} FAIL {type(exc).__name__}: {str(exc)[:80]}", flush=True)
                if consecutive >= 3:
                    print("連続失敗が上限 — 中断 (rc=1)", file=sys.stderr)
                    return 1
                continue
            consecutive = 0
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
    if ok == 0:
        # 1 件も捕獲できないのは経路の不整合 (marker 変更 / 呼出先の変更)。
        # 実害 2026-09-06: 19 窓を回して 0 件だったのに rc=0 で「完了」と記録された。
        print("⚠ 捕獲 0 件 — 教師の挿し先か marker を疑う (rc=1)", file=sys.stderr)
        return 1
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
