"""S の切替前に、学習集合の S 由来の特徴を新しい S で付け直す (S-SWITCH-RETRAIN、2026-11)。

付け直すのは深刻度の軸 (``severity_axes.classify_axes``、本文入力 = D1 の型) と深刻さ C の
9 問の確率 (``severity_c.probe_questions``、``decision_probe``)。種別・対の判定は
``scripts/s_switch_retrain.py`` の ``_relabel_kinds`` / ``_relabel_pairs``。

**出力は staging の jsonl だけ** (本番の表・config/models/*.json・割当は書かない)。行の形は
既存の消費者と同じ:

* 軸: ``{"article_id", "model", "axes", "seconds"}`` (``data/mlx/axes_body_backfill.py`` と同形。
  ``staging_inputs.load_axes_jsonl`` / ``train_detect_ml.py --axes`` が読む)
* 確率: ``{"article_id", "p_yes", "probe_model", "missing", "from"}``
  (``data/mlx/sevc_ci65_probe.py`` と同形。``train_severity_s3_model.py --probe-file`` が読む)

再開可 (済みの記事は飛ばす)。失敗した軸は書かない (欠測で次回に再試行)。``limit`` と
``max_seconds`` で試走を短く切れる (本番が動いている時間に Ollama を長く占有しない)。
LLM 呼び出しは引数の関数で受け取るので、テストは偽物を渡せる。
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

#: 軸 1 件 (title, summary, body, published) → 軸の dict。失敗は None
AxesFn = Callable[[str, str, str, str | None], Awaitable[Mapping[str, Any] | None]]
#: 確率 1 件 (title, published, body) → 問い id → P(はい) (失敗した問いは None)
ProbeFn = Callable[[str, str | None, str], Awaitable[Mapping[str, float | None]]]

PROBE_FROM: Final = "probe"


@dataclass(frozen=True)
class RelabelItem:
    """付け直しの入力 1 件 (本文は DB / items.jsonl から。ログ・出力 jsonl には書かない)。"""

    article_id: str
    title: str
    summary: str
    body: str
    published: str | None


@dataclass(frozen=True)
class RelabelResult:
    written: int
    failed: int
    skipped_done: int
    skipped_no_body: int
    stopped_early: bool


def read_done_ids(path: Path) -> set[str]:
    """出力 jsonl で既に済んだ記事 id (無ければ空)。壊れた行は読み飛ばす (再開で書き直す)。"""
    if not path.exists():
        return set()
    done: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            done.add(str(json.loads(line)["article_id"]))
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    return done


def axes_row(
    article_id: str, model: str, axes: Mapping[str, Any], seconds: float
) -> dict[str, Any]:
    return {
        "article_id": article_id,
        "model": model,
        "axes": dict(axes),
        "seconds": round(seconds, 1),
    }


def probe_row(
    article_id: str, model: str, probs: Mapping[str, float | None], question_ids: Sequence[str]
) -> dict[str, Any]:
    missing = sum(probs.get(q) is None for q in question_ids)
    return {
        "article_id": article_id,
        "p_yes": {q: probs.get(q) for q in question_ids},
        "probe_model": model,
        "missing": missing,
        "from": PROBE_FROM,
    }


def items_from_sevc_rows(rows: Iterable[Mapping[str, Any]]) -> list[RelabelItem]:
    """``data/eval/sevc_v5/items.jsonl`` の行 → 入力。

    本文が無ければ要約 (sevc_ci65_probe と同じ)。
    """
    return [
        RelabelItem(
            article_id=str(r["article_id"]),
            title=str(r.get("title") or ""),
            summary=str(r.get("summary") or ""),
            body=str(r.get("body") or r.get("summary") or ""),
            published=(str(r["published"]) if r.get("published") else None),
        )
        for r in rows
    ]


class _Budget:
    def __init__(self, limit: int, max_seconds: float, clock: Callable[[], float]) -> None:
        self._limit = limit
        self._deadline = clock() + max_seconds if max_seconds > 0 else None
        self._clock = clock
        self.attempted = 0

    def exhausted(self) -> bool:
        if self._limit and self.attempted >= self._limit:
            return True
        return self._deadline is not None and self._clock() >= self._deadline


async def relabel_axes(
    items: Sequence[RelabelItem],
    *,
    model: str,
    out: Path,
    classify: AxesFn,
    limit: int = 0,
    max_seconds: float = 0.0,
    clock: Callable[[], float] = time.monotonic,
) -> RelabelResult:
    """軸を本文入力で付け直す。

    本文の無い記事は飛ばす (要約入力に倒れて分布が混ざるため、D1 と同じ)。
    """
    done = read_done_ids(out)
    todo = [it for it in items if it.article_id not in done and it.body]
    no_body = sum(1 for it in items if it.article_id not in done and not it.body)
    budget = _Budget(limit, max_seconds, clock)
    written = failed = 0
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as fh:
        for it in todo:
            if budget.exhausted():
                break
            budget.attempted += 1
            started = clock()
            axes = await classify(it.title, it.summary, it.body, it.published)
            if axes is None:
                failed += 1
                continue
            row = axes_row(it.article_id, model, axes, clock() - started)
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
            written += 1
    return RelabelResult(
        written=written,
        failed=failed,
        skipped_done=len(done & {it.article_id for it in items}),
        skipped_no_body=no_body,
        stopped_early=budget.attempted < len(todo),
    )


async def relabel_probes(
    items: Sequence[RelabelItem],
    *,
    model: str,
    out: Path,
    probe: ProbeFn,
    question_ids: Sequence[str],
    limit: int = 0,
    max_seconds: float = 0.0,
    clock: Callable[[], float] = time.monotonic,
) -> RelabelResult:
    """深刻さ C の 9 問を当て直す。

    欠けた問いも行に残す (``missing``、学習側が ``--max-missing`` で除く)。
    """
    done = read_done_ids(out)
    todo = [it for it in items if it.article_id not in done]
    budget = _Budget(limit, max_seconds, clock)
    written = failed = 0
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as fh:
        for it in todo:
            if budget.exhausted():
                break
            budget.attempted += 1
            probs = await probe(it.title, it.published, it.body)
            row = probe_row(it.article_id, model, probs, question_ids)
            if row["missing"] == len(question_ids):
                failed += 1  # 全問失敗 (通信断等) は書かない = 次回に再試行
                continue
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
            written += 1
    return RelabelResult(
        written=written,
        failed=failed,
        skipped_done=len(done & {it.article_id for it in items}),
        skipped_no_body=0,
        stopped_early=budget.attempted < len(todo),
    )
