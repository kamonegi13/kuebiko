#!/usr/bin/env python3
"""状況総括 (render 段) の SFT 教師対を外部 LLM で生成する (CoT 蒸留用、2026-09-15 再設計)。

方式: 過去窓の **保存済み estimate** (``status_synthesis.tradecraft.grounded_estimate``) から
本番と同じ seam (``build_render_plan``) で render プロンプトを再構築し、教師に **1 呼出だけ**
投げる。パイプラインは再実行しない — 入力は本番がその日に実際に射影した estimate そのもの、
外部枠は目的物 (render) にしか使わない、GPU も要らない。

⚠ **旧方式 (``generate_synthesis(now=過去日付)`` の再生) は禁止**。台帳駆動
(``SYNTHESIS_STATE=1``) では過去 now で ``build_estimate_stateful`` が走り、revision /
証拠の既読マーク / 検出ログを**過去時刻で本番台帳に書き込む** (pipeline.py が「revision 順序を
壊すため禁止」と明記する経路)。2026-09-06 の収穫で実際に起き、revision 95 件 (うち 41 件が
今も最新判定) ・既読マーク 133 件・検出ログ 166 件を汚染した。本スクリプトは
``generate_synthesis`` を import しない (静的に経路を断つ)。

不変条件:
- **凍結審判の窓は収穫しない**: ``data/mlx/synthesis_judge_set*.json`` に載る key と、
  直近 ``--eval-reserve-days`` 日を除外する (重なると生徒は答えを見て答える)。
- **長さは収穫時に制御する**: MLX 学習の系列長メモリ壁は ~14.1k トークン (15.1k で OOM)。
  daily 78 窓の render プロンプトは中央 9.0k / 最大 16.0k トークン (直近ほど長い)。
  completion は本番実測 中央 2,110 字 ≒ 1.1k トークン (教師はこれより長く CoT 欄も乗るので
  2.5k を見込む)。予算超過の prompt は教師に投げない (外部枠を使わない)。
  トークン見積りは実測比 ``chars / 1.85`` (15 窓で 1.76-1.91、Gemma-4 26B tokenizer)。
  最終的な足切りは ``assemble_sft_dataset.py --max-tokens`` が正確なトークン数で行う。
- ``--cot`` では ``analysis_notes`` が空の completion を採らない (目的物が無い = 経路不整合)。
- 1 件も採れなければ rc=1 (2026-09-06 の「0 件で完了と記録」の再発防止)。

使用例 (ホストでも コンテナでも可。DB 読みと外部 1 呼出/窓のみ):
    docker exec -e LLM_LOCAL_FALLBACK=0 kuebiko python \\
        scripts/build_sft_teacher_synthesis.py --model claudecode:opus --cot --dry-run
    docker exec -e LLM_LOCAL_FALLBACK=0 kuebiko python \\
        scripts/build_sft_teacher_synthesis.py --model claudecode:opus --cot
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config_loader import load_app_config  # noqa: E402
from src.storage.run_history import RunHistoryRepository, StatusSynthesisRecord  # noqa: E402
from src.synthesis.generator import _resolve_period  # noqa: E402
from src.synthesis.grounded.estimate import Estimate, estimate_from_dict  # noqa: E402
from src.synthesis.grounded.render import (  # noqa: E402
    _MAX_TOKENS,
    _TEMPERATURE,
    _WireSections,
    _WireSectionsCoT,
    build_render_plan,
)
from src.tools.llm_client import LLMClient  # noqa: E402
from src.tools.model_tiers import Step, build_llm_for_ref  # noqa: E402

DEFAULT_OUT = Path("data/mlx/teacher/synthesis.jsonl")
JUDGE_SET_GLOB = "data/mlx/synthesis_judge_set*.json"
_MIN_COMPLETION_CHARS = 400  # これ未満の narrative は教師として保存しない
_MIN_JUDGMENTS = 2  # 判定がこれ未満の窓は教師にしない (静穏すぎて学ぶものが無い)
#: 文字数 → トークン数の実測比 (2026-09-15、凍結 15 窓 / Gemma-4 26B tokenizer で 1.76-1.91)。
_CHARS_PER_TOKEN = 1.85


def _est_tokens(text: str) -> int:
    """文字数からトークン数を見積もる (コンテナに tokenizer を持ち込まないための近似)。"""
    return int(len(text) / _CHARS_PER_TOKEN)


def _has_notes(completion: str) -> bool:
    """completion (JSON 文字列) に非空の analysis_notes があるか。"""
    try:
        data = json.loads(completion)
    except json.JSONDecodeError:
        return False
    return bool(isinstance(data, dict) and str(data.get("analysis_notes", "")).strip())


def _estimate_of(rec: StatusSynthesisRecord) -> Estimate | None:
    """保存記録から grounded_estimate を取り出す (無い/壊れは None)。"""
    if not rec.tradecraft:
        return None
    try:
        data = json.loads(rec.tradecraft)
    except json.JSONDecodeError:
        return None
    raw = data.get("grounded_estimate") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return None
    try:
        return estimate_from_dict(raw)
    except (KeyError, TypeError, ValueError):
        return None


def _window_key(period_type: str, est: Estimate) -> str:
    return f"synth:{period_type}:{est.period_start.date().isoformat()}"


def reserved_keys(judge_set_paths: list[Path]) -> set[str]:
    """凍結審判セットに載る窓 key (収穫から除外する)。"""
    keys: set[str] = set()
    for path in judge_set_paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        keys.update(str(i.get("key", "")) for i in payload.get("items", []) if isinstance(i, dict))
    return keys


@dataclass(frozen=True)
class Window:
    key: str
    prompt: str
    judgments: int


def select_windows(
    records: list[StatusSynthesisRecord],
    *,
    period_type: str,
    reserved: set[str],
    reserve_before: datetime,
    cot: bool,
    min_judgments: int = _MIN_JUDGMENTS,
) -> tuple[list[Window], dict[str, int]]:
    """収穫対象の窓を選び、本番と同じ seam でプロンプトを組む (LLM 呼出なし)。

    返り値 = (窓, 除外理由ごとの件数)。除外は黙らせない (歩留まりの説明に要る)。
    """
    out: list[Window] = []
    skipped = {"no_estimate": 0, "thin": 0, "reserved_key": 0, "reserved_recent": 0}
    for rec in records:
        est = _estimate_of(rec)
        if est is None:
            skipped["no_estimate"] += 1
            continue
        key = _window_key(period_type, est)
        if key in reserved:
            skipped["reserved_key"] += 1
            continue
        if est.period_start >= reserve_before:
            skipped["reserved_recent"] += 1
            continue
        if len(est.judgments) < min_judgments:
            skipped["thin"] += 1
            continue
        # 期間ラベルは本番と同じ導出 (daily は「当日 00:00 JST 〜 period_end JST」)
        _s, _e, label, _lb, _bw = _resolve_period(period_type=period_type, now=est.period_end)
        plan = build_render_plan(est=est, period_label=label, cot_notes=cot)
        out.append(Window(key=key, prompt=plan.prompt, judgments=len(est.judgments)))
    return out, skipped


def accept_completion(
    prompt: str, completion: str, *, cot: bool, max_pair_tokens: int
) -> str | None:
    """保存してよい completion なら None、駄目なら理由を返す。"""
    if len(completion) < _MIN_COMPLETION_CHARS:
        return "short"
    if cot and not _has_notes(completion):
        return "no_notes"
    if _est_tokens(prompt) + _est_tokens(completion) > max_pair_tokens:
        return "pair_too_long"
    return None


def _done_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {
        json.loads(line)["key"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


async def harvest(
    teacher: LLMClient | None,
    windows: list[Window],
    *,
    out: Path,
    cot: bool,
    max_prompt_tokens: int,
    max_pair_tokens: int,
    dry_run: bool,
) -> dict[str, int]:
    """窓ごとに教師を 1 呼出。集計 (採用/理由別の不採用) を返す。"""
    schema: type[_WireSections] | type[_WireSectionsCoT] = (
        _WireSectionsCoT if cot else _WireSections
    )
    done = _done_keys(out)
    stats = {
        "ok": 0,
        "done": 0,
        "oversize": 0,
        "failed": 0,
        "short": 0,
        "no_notes": 0,
        "pair_too_long": 0,
    }
    consecutive = 0
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as fh:
        for w in windows:
            if w.key in done:
                stats["done"] += 1
                continue
            if _est_tokens(w.prompt) > max_prompt_tokens:
                # 学習に使えない長さの対を外部枠で作らない (収穫時に落とす)
                stats["oversize"] += 1
                print(
                    f"  {w.key} prompt ≈{_est_tokens(w.prompt)} tok — 予算超過でスキップ",
                    flush=True,
                )
                continue
            if dry_run:
                print(
                    f"  {w.key} prompt ≈{_est_tokens(w.prompt)} tok / 判定 {w.judgments} (dry-run)"
                )
                stats["ok"] += 1
                continue
            if teacher is None:
                raise RuntimeError("teacher が無い (dry_run 以外では必須)")
            try:
                result = await teacher.generate_structured(
                    w.prompt, schema, temperature=_TEMPERATURE, max_tokens=_MAX_TOKENS, think=False
                )
            except Exception as exc:  # noqa: BLE001 — 1 窓の失敗で全体を落とさない
                stats["failed"] += 1
                consecutive += 1
                print(f"  {w.key} FAIL {type(exc).__name__}: {str(exc)[:80]}", flush=True)
                if consecutive >= 3:
                    print("連続失敗が上限 — 中断", file=sys.stderr)
                    break
                continue
            consecutive = 0
            completion = json.dumps(result.model_dump(), ensure_ascii=False)
            reason = accept_completion(
                w.prompt, completion, cot=cot, max_pair_tokens=max_pair_tokens
            )
            if reason is not None:
                stats[reason] += 1
                print(f"  {w.key} 不採用 ({reason})", flush=True)
                continue
            fh.write(
                json.dumps(
                    {"key": w.key, "prompt": w.prompt, "completion": completion}, ensure_ascii=False
                )
                + "\n"
            )
            fh.flush()
            stats["ok"] += 1
            print(f"  {w.key} 採用 (completion {len(completion)} 字)", flush=True)
    return stats


async def main_async(args: argparse.Namespace) -> int:
    # dry-run は教師を組まない (bridge/API キー無しでも対象窓と長さを確認できる)
    teacher: LLMClient | None = (
        None
        if args.dry_run
        else build_llm_for_ref(args.model, Step.SYNTHESIS_NARRATIVE, load_app_config())
    )
    repo = RunHistoryRepository()
    records = repo.list_synthesis(period_type=args.period, limit=args.scan)
    reserved = reserved_keys(sorted(Path().glob(JUDGE_SET_GLOB)))
    reserve_before = datetime.now(UTC) - timedelta(days=args.eval_reserve_days)
    windows, skipped = select_windows(
        records,
        period_type=args.period,
        reserved=reserved,
        reserve_before=reserve_before,
        cot=args.cot,
    )
    print(
        f"窓 {len(windows)} (除外: 審判 key {skipped['reserved_key']} / "
        f"直近 {args.eval_reserve_days} 日 {skipped['reserved_recent']} / "
        f"estimate 無し {skipped['no_estimate']} / 判定不足 {skipped['thin']})",
        file=sys.stderr,
    )
    stats = await harvest(
        teacher,
        windows,
        out=args.out,
        cot=args.cot,
        max_prompt_tokens=args.max_prompt_tokens,
        max_pair_tokens=args.max_pair_tokens,
        dry_run=args.dry_run,
    )
    print(f"\n完了: {stats} → {args.out}")
    if stats["ok"] == 0 and not args.dry_run:
        print("⚠ 採用 0 件 — 経路か予算を疑う (rc=1)", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--model", required=True, help="教師モデル ref (例 claudecode:opus)")
    ap.add_argument("--period", default="daily")
    ap.add_argument("--scan", type=int, default=200, help="新しい順に走査する保存記録の数")
    ap.add_argument(
        "--eval-reserve-days", type=int, default=15, help="直近この日数は凍結評価用に予約"
    )
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
    ap.add_argument("--dry-run", action="store_true", help="教師を呼ばず、対象窓と長さだけ印字する")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
