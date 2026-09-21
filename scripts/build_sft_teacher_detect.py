#!/usr/bin/env python3
"""detect (台帳の開設候補の選定) の SFT 教師対を外部 LLM で作る (2026-09-21)。

背景: detect は **s17 にも n17c にも学習させていない課題**だった (教師データ一覧に無い)。
4 腕比較は未学習どうしの比較で、差は他課題からの偶然の転移。教師は測定で決めた:
Sonnet が事象単位の回収 26 で最多・誤り率 22% (26b と同水準)・固有回収 5。

⭐ **本番と同じ入力・同じ 2 呼出**を教師に投げ、(prompt, completion) をそのまま残す:

| 呼出 | 入力 | template |
|---|---|---|
| cur | ML 前段で絞った候補 (top-k + 下限保証 − 一括勧告) | `synthesis/detect_new.j2` |
| ml_add | cur が開かなかった ML 上位 (和集合の追加分) | `synthesis/detect_ml_select.j2` |

⚠ 群化 (event_items) には一切依存しない — detect は articles だけを読む (2026-09-21 に
grep で確認)。統合の本番適用と並行して収穫できる。

⚠ **凍結評価に使った replay の 5 日は収穫しない** (`EVAL_HOLDOUT_DAYS`)。生徒の合否は
そこで測る。⚠ `active_titles` (台帳の現在の追跡対象) は収穫時点のもので、過去日の
as-of ではない (replay と同じ制約)。

⚠ **外部が落ちたら黙ってローカルへ倒さない** (`LLM_LOCAL_FALLBACK=0` を既定にする。
倒れると教師対にローカルの出力が混ざる)。連続 3 失敗で止まり、再実行は処理済みの
key を読み飛ばす。1 件も採れなければ rc=1。

    docker exec -e LLM_LOCAL_FALLBACK=0 kuebiko python \\
        scripts/build_sft_teacher_detect.py --model claudecode:sonnet
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from scripts.replay_detect_prefilter import _kinds, narrow, pool_for_day  # noqa: E402
from src.assessment.situation_store import SituationStore  # noqa: E402
from src.config_loader import load_app_config  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.synthesis.grounded.detect_ml import (  # noqa: E402
    build_detect_articles,
    is_rollup_title,
    load_detect_model,
    prefilter_top_k,
    score_articles,
    union_additions,
    union_top_k,
)
from src.synthesis.grounded.incremental import detect_new_claims  # noqa: E402
from src.tools.llm_client import LLMClient, LLMError  # noqa: E402
from src.tools.model_tiers import Step, build_llm_for_ref  # noqa: E402

OUT = Path("data/mlx/teacher/detect.jsonl")
#: 凍結評価 (replay の 5 日、プールが大きい順) — 収穫しない
EVAL_HOLDOUT_DAYS: frozenset[str] = frozenset(
    {"2026-08-18", "2026-08-20", "2026-08-31", "2026-09-02", "2026-09-03"}
)
_MIN_POOL = 40
_MAX_CONSECUTIVE_FAILURES = 3
_T = TypeVar("_T", bound=BaseModel)


class RecordingClient:
    """(prompt, 構造化出力 JSON) を捕獲する透過ラッパ (ach / spotlight 教師と同型)。"""

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner
        self.last: tuple[str, str] | None = None

    @property
    def model(self) -> str:
        return str(getattr(self._inner, "model", ""))

    async def generate_structured(self, prompt: str, schema: type[_T], **kw: Any) -> _T:
        out = await self._inner.generate_structured(prompt, schema, **kw)
        self.last = (prompt, out.model_dump_json())
        return out


def plan_days(
    *,
    since: str,
    until: str,
    exclude: frozenset[str] = EVAL_HOLDOUT_DAYS,
    done: frozenset[str] = frozenset(),
    newest_first: bool = False,
) -> list[str]:
    """収穫する日を決める (純粋関数)。除外日と処理済み (key の日付部分) を落とす。"""
    done_days = {k.split(":", 1)[0] for k in done}
    d0, d1 = date.fromisoformat(since), date.fromisoformat(until)
    days = [str(d0 + timedelta(days=i)) for i in range((d1 - d0).days + 1)]
    out = [d for d in days if d not in exclude and d not in done_days]
    return list(reversed(out)) if newest_first else out


def teacher_row(
    *, day: str, arm: str, template: str, prompt: str, completion: str, n_candidates: int
) -> dict[str, Any]:
    """教師対 1 行。prompt と completion は **そのまま** (本番と同じ形で学習する)。"""
    parsed = json.loads(completion)
    return {
        "key": f"{day}:{arm}",
        "template": template,
        "prompt": prompt,
        "completion": completion,
        "n_candidates": n_candidates,
        "n_open": len(parsed.get("open", [])),
        "n_rejected": len(parsed.get("rejected", [])),
    }


async def harvest_day(
    *,
    day: str,
    repo: RunHistoryRepository,
    rec: RecordingClient,
    model: Any,
    kinds: dict[str, str],
    active_titles: list[str],
    pir_context: list[dict[str, str]],
) -> list[dict[str, Any]]:
    pool = pool_for_day(repo, day)
    if len(pool) < _MIN_POOL:
        return []
    arts = build_detect_articles(repo, [str(a["article_id"]) for a in pool], kinds)
    scores = score_articles(model, arts) if model is not None else {}
    cand = narrow(pool, scores, arts, top_k=prefilter_top_k()) if model is not None else pool
    rows: list[dict[str, Any]] = []
    base = await detect_new_claims(
        llm=rec,  # type: ignore[arg-type]
        articles=cand,
        active_titles=active_titles,
        pir_context=pir_context,
        period_label=f"{day} (teacher)",
        template="synthesis/detect_new.j2",
    )
    assert rec.last is not None
    rows.append(
        teacher_row(
            day=day,
            arm="cur",
            template="synthesis/detect_new.j2",
            prompt=rec.last[0],
            completion=rec.last[1],
            n_candidates=len(cand),
        )
    )
    if not scores:
        return rows
    roll = {a for a in scores if a in arts and is_rollup_title(arts[a].title)}
    picks = set(
        union_additions(
            {
                str(a["article_id"]): scores[str(a["article_id"])]
                for a in cand
                if str(a["article_id"]) in scores
            },
            top_k=union_top_k(),
            already_opened={a for c in base.open for a in c.article_ids},
            excluded=roll,
        )
    )
    add = [a for a in cand if str(a["article_id"]) in picks]
    if not add:
        return rows
    rec.last = None
    await detect_new_claims(
        llm=rec,  # type: ignore[arg-type]
        articles=add,
        active_titles=active_titles,
        pir_context=pir_context,
        period_label=f"{day} (teacher)",
        template="synthesis/detect_ml_select.j2",
    )
    assert rec.last is not None
    rows.append(
        teacher_row(
            day=day,
            arm="ml_add",
            template="synthesis/detect_ml_select.j2",
            prompt=rec.last[0],
            completion=rec.last[1],
            n_candidates=len(add),
        )
    )
    return rows


async def main_async(args: argparse.Namespace) -> int:
    os.environ.setdefault("LLM_LOCAL_FALLBACK", "0")  # ⚠ 教師にローカルの出力を混ぜない
    repo = RunHistoryRepository()
    teacher = build_llm_for_ref(
        args.model, Step.SYNTHESIS_DETECT, load_app_config(), timeout_seconds=900.0
    )
    rec = RecordingClient(teacher)
    print(f"教師モデル: {rec.model}", flush=True)
    done: frozenset[str] = frozenset()
    if OUT.exists() and not args.fresh:
        done = frozenset(
            json.loads(x)["key"] for x in OUT.read_text(encoding="utf-8").splitlines() if x.strip()
        )
        print(f"既存 {len(done)} 対を読み飛ばす", flush=True)
    days = plan_days(since=args.since, until=args.until, done=done, newest_first=args.newest_first)
    if args.max_days:
        days = days[: args.max_days]
    model = load_detect_model()
    if model is None:
        print("⚠ detect ML モデルが無い — 候補は全プール (本番と入力が違う)", flush=True)
    kinds = _kinds()
    store = SituationStore(db_path=Path("data/run_history.db"))
    active_titles = [r.title for r in store.load_situations(("active",))]
    try:
        from src.pir.integration import build_synthesis_pir_context, get_pir_config

        pir_context = build_synthesis_pir_context(get_pir_config().priorities)
    except Exception:  # noqa: BLE001 — PIR 不在でも収穫は成立する
        pir_context = []
    print(f"対象 {len(days)} 日 ({days[0] if days else '-'} .. {days[-1] if days else '-'})")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    total = failures = 0
    for day in days:
        try:
            rows = await harvest_day(
                day=day,
                repo=repo,
                rec=rec,
                model=model,
                kinds=kinds,
                active_titles=active_titles,
                pir_context=pir_context,
            )
        except (LLMError, OSError) as exc:
            failures += 1
            print(f"{day}  ⚠ 失敗 {type(exc).__name__}: {str(exc)[:120]}", flush=True)
            if failures >= _MAX_CONSECUTIVE_FAILURES:
                print("連続失敗で停止 (外部枠切れの疑い。再実行で続きから)", flush=True)
                break
            continue
        failures = 0
        if not rows:
            print(f"{day}  プール不足 — 飛ばす", flush=True)
            continue
        with OUT.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        total += len(rows)
        cur = rows[0]
        print(
            f"{day}  候補 {cur['n_candidates']:3d} → 開設 {cur['n_open']} "
            f"見送り {cur['n_rejected']}"
            f"{'  +ml_add ' + str(rows[1]['n_open']) if len(rows) > 1 else ''}  (累計 {total})",
            flush=True,
        )
    print(f"書込: {OUT} (+{total} 対)", flush=True)
    return 0 if total else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", default="claudecode:sonnet")
    p.add_argument("--since", default="2026-06-01")
    p.add_argument("--until", default="2026-09-20")
    p.add_argument("--max-days", type=int, default=0, help="0 = 全日")
    p.add_argument("--newest-first", action="store_true")
    p.add_argument("--fresh", action="store_true")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
