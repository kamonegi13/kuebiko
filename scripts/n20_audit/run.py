"""T3/T4 の Opus 審査を回す (結果はキャッシュされるので再実行で続きから)。

    uv run python -m scripts.n20_audit.run t4           # T4: tr_raw 30 チャンクの監査 (1 回目)
    uv run python -m scripts.n20_audit.run t3-pass1     # T3: n19 の基線 (内容の監査 1 回目)
    uv run python -m scripts.n20_audit.run t3-pass2     # T3: 自己一致の 2 回目 (内容 + 翻訳)
    uv run python -m scripts.n20_audit.run t3-confirm   # T3: 1 回目の重い指摘の確認
    --dry-run で Opus を呼ばずに件数とキャッシュの当たりだけを数える。

終了コード: 0 = 完了、2 = 上限 (回数の上限・連続失敗) で止めた (再実行で続きから)。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from scripts.n20_audit import rubric as rb
from scripts.n20_audit.common import (
    N20_TEACHER,
    T3_DIR,
    T4_DIR,
    TASKS_T3,
    append_jsonl,
    read_jsonl,
)
from scripts.n20_audit.opus import OpusBudget, OpusStopError, budget_for, judge
from scripts.n20_audit.stats import locate_quote

CONCURRENCY = 3
CONTENT_MAX_TOKENS = 4000
TRANSLATION_MAX_TOKENS = 2048
CONFIRM_MAX_TOKENS = 2000
TRANSLATION_RUBRIC_TAG = "c2c-v1"
LEDGER = T3_DIR / "opus_ledger.jsonl"


def content_rubric() -> str:
    return f"[rubric {rb.RUBRIC_ID}]\n{rb.CONTENT_RUBRIC}"


def confirm_rubric() -> str:
    return f"[rubric {rb.CONFIRM_ID}]\n{rb.CONFIRM_RUBRIC}"


def translation_rubric(kind: str) -> str:
    """tr_audit.py と同じ c2c-v1 の指示 (課題ごとの本番のシステムプロンプト込み)。"""
    sys.path.insert(0, str(N20_TEACHER))
    import tr_audit  # type: ignore[import-not-found]  # noqa: PLC0415

    text: str = tr_audit.rubric_for(kind, TRANSLATION_RUBRIC_TAG)
    return text


def content_inputs(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_note": rb.task_note(item["task"]),
        "material_complete": item["material_complete"],
        "material": item["material"],
        "output": item["output"],
    }


def _items_t3() -> list[dict[str, Any]]:
    return [i for t in TASKS_T3 for i in read_jsonl(T3_DIR / f"items_{t}.jsonl")]


def _items_t4() -> list[dict[str, Any]]:
    return read_jsonl(T4_DIR / "items.jsonl")


def _done(path: Any) -> set[str]:
    return {r["item_id"] for r in read_jsonl(path)}


async def _gather(jobs: Sequence[Callable[[], Awaitable[None]]]) -> None:
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(job: Callable[[], Awaitable[None]]) -> None:
        async with sem:
            await job()

    await asyncio.gather(*[one(j) for j in jobs])


async def _run_content(pass_no: int, budget: OpusBudget, dry: bool) -> None:
    out = T3_DIR / f"audit_pass{pass_no}.jsonl"
    done = _done(out)
    todo = [i for i in _items_t3() if i["item_id"] not in done]
    print(f"内容 pass{pass_no}: 済み {len(done)} 残り {len(todo)}", flush=True)
    if dry:
        return

    def job(item: dict[str, Any]) -> Callable[[], Awaitable[None]]:
        async def run() -> None:
            v = await judge(
                rb.content_question(pass_no),
                content_inputs(item),
                rb.ContentAudit,
                rubric=content_rubric(),
                budget=budget,
                max_tokens=CONTENT_MAX_TOKENS,
            )
            if v is None:
                return
            append_jsonl(
                out,
                {
                    "item_id": item["item_id"],
                    "task": item["task"],
                    "pass": pass_no,
                    "time": datetime.now(UTC).isoformat(),
                    **v.model_dump(),
                },
            )

        return run

    await _gather([job(i) for i in todo])


async def _run_translation(pass_no: int, budget: OpusBudget, dry: bool) -> None:
    out = T4_DIR / f"audit_pass{pass_no}.jsonl"
    done = _done(out)
    todo = [i for i in _items_t4() if i["item_id"] not in done]
    print(f"翻訳 pass{pass_no}: 済み {len(done)} 残り {len(todo)}", flush=True)
    if dry:
        return

    def job(item: dict[str, Any]) -> Callable[[], Awaitable[None]]:
        async def run() -> None:
            v = await judge(
                rb.TRANSLATION_QUESTIONS[pass_no],
                {"source_en": item["source"], "translation_ja": item["output"]},
                _translation_schema(),
                rubric=translation_rubric(item["kind"]),
                budget=budget,
                max_tokens=TRANSLATION_MAX_TOKENS,
            )
            if v is None:
                return
            append_jsonl(
                out,
                {
                    "item_id": item["item_id"],
                    "kind": item["kind"],
                    "pass": pass_no,
                    "time": datetime.now(UTC).isoformat(),
                    **v.model_dump(),
                },
            )

        return run

    await _gather([job(i) for i in todo])


def _translation_schema() -> type[BaseModel]:
    sys.path.insert(0, str(N20_TEACHER.parent))
    from measure_c2c import TranslationAudit  # type: ignore[import-not-found]  # noqa: PLC0415

    schema: type[BaseModel] = TranslationAudit
    return schema


def heavy_to_confirm(item: dict[str, Any], audit: dict[str, Any]) -> list[dict[str, Any]]:
    """1 回目の重い指摘のうち、引用が出力に実在するもの (確認に回す)。id = f<番号>。"""
    out = []
    for n, f in enumerate(audit["findings"]):
        if f["severity"] != "重い":
            continue
        ok, idx = locate_quote(f["quote"], item["claims"])
        if ok:
            out.append(
                {
                    "id": f"f{n}",
                    "quote": f["quote"],
                    "kind": f["kind"],
                    "reason": f["reason"],
                    "claim_idx": idx,
                }
            )
    return out


async def _run_confirm(budget: OpusBudget, dry: bool) -> None:
    out = T3_DIR / "confirm.jsonl"
    done = _done(out)
    items = {i["item_id"]: i for i in _items_t3()}
    todo = []
    for a in read_jsonl(T3_DIR / "audit_pass1.jsonl"):
        if a["item_id"] in done:
            continue
        cands = heavy_to_confirm(items[a["item_id"]], a)
        if cands:
            todo.append((items[a["item_id"]], cands))
    print(
        f"確認: 済み {len(done)} 残り {len(todo)} 件 (指摘 {sum(len(c) for _, c in todo)})",
        flush=True,
    )
    if dry:
        return

    def job(item: dict[str, Any], cands: list[dict[str, Any]]) -> Callable[[], Awaitable[None]]:
        async def run() -> None:
            inputs = {
                **content_inputs(item),
                "findings": [{k: c[k] for k in ("id", "quote", "kind", "reason")} for c in cands],
            }
            v = await judge(
                rb.confirm_question(),
                inputs,
                rb.ConfirmVerdict,
                rubric=confirm_rubric(),
                budget=budget,
                max_tokens=CONFIRM_MAX_TOKENS,
            )
            if v is None:
                return
            append_jsonl(
                out,
                {
                    "item_id": item["item_id"],
                    "task": item["task"],
                    "time": datetime.now(UTC).isoformat(),
                    "asked": cands,
                    **v.model_dump(),
                },
            )

        return run

    await _gather([job(i, c) for i, c in todo])


async def _main(cmd: str, dry: bool) -> int:
    stages: dict[str, tuple[str, Callable[[OpusBudget], Awaitable[None]]]] = {
        "t4": ("T4", lambda b: _run_translation(1, b, dry)),
        "t3-pass1": ("T3-BASE", lambda b: _run_content(1, b, dry)),
        "t3-pass2": (
            "T3-SELF",
            lambda b: _seq(_run_content(2, b, dry), _run_translation(2, b, dry)),
        ),
        "t3-confirm": ("T3-CONFIRM", lambda b: _run_confirm(b, dry)),
    }
    stage, fn = stages[cmd]
    budget = budget_for(stage)
    code = 0
    try:
        await fn(budget)
    except OpusStopError as exc:
        print("止めた:", exc, flush=True)
        code = 2
    if not dry:
        append_jsonl(
            LEDGER,
            {"time": datetime.now(UTC).isoformat(), "cmd": cmd, "exit": code, **budget.as_dict()},
        )
    print(budget.as_dict(), flush=True)
    return code


async def _seq(*coros: Awaitable[None]) -> None:
    for c in coros:
        await c


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["t4", "t3-pass1", "t3-pass2", "t3-confirm"])
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    return asyncio.run(_main(a.cmd, a.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
