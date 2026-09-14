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

``--cot`` で ``analysis_notes`` (思考を本文の前に書かせる欄) つきの教師を収穫する
(CoT 蒸留用)。env flag ``SYNTHESIS_COT_NOTES=1`` を立てて本番経路の schema を切り替える
ため、**プロンプトと出力の形は配備時と同じ**になる。

不変条件:
- 直近 ``--eval-reserve-days`` 日は凍結評価用に予約 (収穫しない)。凍結審判セットを作った
  ``build_synthesis_judge_set.py`` が「必要な予約日数」を印字するので、その値以上を渡す。
- ``LLM_LOCAL_FALLBACK=0`` で実行 (ローカル出力の教師混入防止)。
- narrative 段が複数回呼ばれる実装変更に備え、捕獲は list (全呼出を保存)。
- **長さは収穫時に制御する** (下記)。学習に使えない長さの対を外部枠で作らない。

長さ制御 (2026-09-15 実測に基づく): MLX 学習の系列長メモリ壁は ~14.1k トークン
(15.1k で OOM)。daily 78 窓の render プロンプトは中央 9.0k / 最大 16.0k トークン
(直近ほど長い — 凍結 15 窓は中央 11.3k / 最大 14.2k)。completion 側は本番実測で
中央 2,110 字 ≒ 1.1k トークン (教師はこれより長く、CoT 欄も乗るので 2.5k を見込む)。
**そのままでは pair (prompt + completion) が壁を超える窓が多い**。そこで
① 生成前に prompt が予算超過なら教師を呼ばずローカルへ流す (外部枠を使わない)
② 生成後に pair 実測見積りが予算超過なら保存しない。
トークン見積りは実測比 ``chars / 1.85`` (15 窓で 1.76-1.91、Gemma-4 26B tokenizer)。
コンテナに transformers を入れないための近似で、最終的な足切りは
``assemble_sft_dataset.py --max-tokens`` が正確なトークン数で行う。

使用例 (コンテナ内・GPU 静穏時):
    docker exec -e LLM_LOCAL_FALLBACK=0 kuebiko python \\
        scripts/build_sft_teacher_synthesis.py --model claudecode:opus --days 80 --cot
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
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
#: 文字数 → トークン数の実測比 (2026-09-15、凍結 15 窓 / Gemma-4 26B tokenizer で 1.76-1.91)。
_CHARS_PER_TOKEN = 1.85


#: render.j2 由来のプロンプトだけを教師へ回すための識別文字列 (テンプレート冒頭の固定句)。
#: 変わると**黙って 0 件になる**ので、実行時に 1 件も捕獲できなければ失敗として返す。
_RENDER_MARKER = "これは射影であって再分析ではない"


def _est_tokens(text: str) -> int:
    """文字数からトークン数を見積もる (コンテナに tokenizer を持ち込まないための近似)。"""
    return int(len(text) / _CHARS_PER_TOKEN)


class SelectiveTeacherClient:
    """render 段だけ教師 (外部) へ、他はローカルへ振り分けて捕獲する。"""

    def __init__(self, teacher: LLMClient, local: LLMClient, *, max_prompt_tokens: int = 0) -> None:
        self._teacher = teacher
        self._local = local
        self._max_prompt_tokens = max_prompt_tokens
        self.captured: list[tuple[str, str]] = []
        #: 予算超過で教師に回さなかった render 呼出の数 (歩留まりの説明に要る)。
        self.oversize = 0

    @property
    def model(self) -> str:
        return self._teacher.model

    def _pick(self, prompt: str) -> tuple[LLMClient, bool]:
        """teacher/local の振り分け。予算超過の render は**教師に回さない**。

        学習に使えない長さの対を外部枠で作っても捨てるだけなので、ここで落とす
        (収穫は収穫時に長さ制御する、2026-09-10 の spotlight 収穫の教訓)。
        """
        is_render = _RENDER_MARKER in prompt
        if is_render and self._max_prompt_tokens and _est_tokens(prompt) > self._max_prompt_tokens:
            self.oversize += 1
            return self._local, False
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


def _has_notes(completion: str) -> bool:
    """captured completion (JSON 文字列) に非空の analysis_notes があるか。"""
    try:
        data = json.loads(completion)
    except json.JSONDecodeError:
        return False
    return bool(isinstance(data, dict) and str(data.get("analysis_notes", "")).strip())


def _done_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {
        json.loads(line)["key"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


async def main_async(args: argparse.Namespace) -> int:
    if args.cot:
        # 本番経路の schema を CoT 版に切り替える (プロンプトも block が 1 つ増える)。
        # import より前に立てる必要はないが、generate_synthesis 呼出より前であること。
        os.environ["SYNTHESIS_COT_NOTES"] = "1"

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
    ok = failed = rejected = oversize = no_notes = too_long = 0
    consecutive = 0
    with args.out.open("a", encoding="utf-8") as fh:
        for date in dates:
            base_key = f"synth:{args.period}:{date.date().isoformat()}"
            if any(k.startswith(base_key) for k in done):
                continue
            rec = SelectiveTeacherClient(
                teacher, local_analysis, max_prompt_tokens=args.max_prompt_tokens
            )
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
            oversize += rec.oversize
            if rec.oversize:
                print(f"  {base_key} 長さ予算超過でスキップ ({rec.oversize} 呼出)", flush=True)
                continue
            if res.record is None or not rec.captured:
                rejected += 1
                print(f"  {base_key} 生成なし ({res.error})", flush=True)
                continue
            for i, (prompt, completion) in enumerate(rec.captured):
                if len(completion) < _MIN_COMPLETION_CHARS:
                    continue
                if args.cot and not _has_notes(completion):
                    # CoT 欄が空 = 収穫の目的物が無い。教師側の schema 切替が効いて
                    # いない可能性があるので、黙って通さず件数に残す。
                    no_notes += 1
                    print(f"  {base_key} analysis_notes が空 — 不採用", flush=True)
                    continue
                if _est_tokens(prompt) + _est_tokens(completion) > args.max_pair_tokens:
                    too_long += 1
                    print(f"  {base_key} pair が長さ予算超過 — 不採用", flush=True)
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

    print(
        f"\n完了: 窓 {ok} / 失敗 {failed} / 生成なし {rejected} / "
        f"長すぎ(生成前) {oversize} / 長すぎ(pair) {too_long} / notes 空 {no_notes} → {args.out}"
    )
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
    ap.add_argument("--cot", action="store_true", help="analysis_notes (CoT) 欄つきで収穫する")
    ap.add_argument(
        "--max-prompt-tokens",
        type=int,
        default=10_500,
        help="これを超える prompt は教師に回さない (既定 = pair 13k − completion 見込み 2.5k)",
    )
    ap.add_argument(
        "--max-pair-tokens",
        type=int,
        default=13_000,
        help="prompt+completion の見積りがこれを超える対は保存しない (MLX の壁 ~14.1k の内側)",
    )
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
