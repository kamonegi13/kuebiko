"""統合で止めた組の週次の抜き出し (2026-11-20、11-19 監査 D3 I-8)。

10-24 に「週 10 組を抜いて Opus で裁定し、区間の下限が 80% を割れば shadow に戻す」と決めたが、
抜き出しが手作業のまま 26 日で 0 回だった。抜き出し (LLM なし) だけをジョブにし、裁定は
手作業の週次に残す (Claude の契約が Pro で、Opus の裁定を自動ジョブにはできない)。

- 書き先: ``data/eval/veto_samples/<YYYY-Www>.jsonl`` (ローカルのみ・gitignore)。
  同じ週のファイルが既にあれば何もしない (再実行で抽出済みの印を重ねない)。
- 裁定の結果は ``labels/<YYYY-Www>.jsonl`` に置く (``scripts/sample_focus_vetoes.py --summarize``)。
- 日次ハートビートに未裁定の週の数を 1 行。最古の未裁定が ``STALE_WEEKS`` 週以上で ⚠。
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.eventnews.veto_sampling import build_sample, week_label
from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)

SAMPLE_DIR_ENV = "VETO_SAMPLE_DIR"
DEFAULT_SAMPLE_DIR = "/app/data/eval/veto_samples"
LABELS_SUBDIR = "labels"
#: 未裁定の週がこの数だけ溜まったら ⚠ (設計 D3 I-8: 裁定 0 回が 4 週続いたら知らせる)
STALE_WEEKS = 4


def sample_dir() -> Path:
    return Path(os.environ.get(SAMPLE_DIR_ENV, DEFAULT_SAMPLE_DIR))


def write_weekly_sample(
    repo: RunHistoryRepository, *, now: datetime, out_dir: Path
) -> dict[str, Any]:
    """1 週分を書き、抜いた組に印を付ける。同期 (呼び手がスレッドへ逃がす)。"""
    label = week_label(now)
    path = out_dir / f"{label}.jsonl"
    if path.exists():
        return {"week": label, "skipped": "exists"}
    picked, lines, pool = build_sample(repo, now=now)
    if not picked:
        return {"week": label, "picked": 0, "pool": pool}
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)
    marked = repo.mark_merge_vetoes_sampled(picked, sampled_at=now)
    return {"week": label, "picked": len(picked), "pool": pool, "marked": marked}


async def run_weekly_veto_sample(repo: RunHistoryRepository | None = None) -> dict[str, Any]:
    """週次ジョブ本体。戻り値は件数だけ (見出しは書き出したファイルにだけ)。"""
    repo = repo or RunHistoryRepository()
    result = await asyncio.to_thread(
        write_weekly_sample, repo, now=datetime.now(UTC), out_dir=sample_dir()
    )
    _log.info("weekly_veto_sample", **result)
    return result


def _week_start(label: str) -> datetime | None:
    try:
        return datetime.strptime(label + "-1", "%G-W%V-%u").replace(tzinfo=UTC)
    except ValueError:
        return None


def _count_lines(path: Path) -> int:
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line)


def build_veto_sample_line(*, now: datetime | None = None, base: Path | None = None) -> str | None:
    """ハートビートの 1 行 (未裁定の週)。抜き出しがまだ無ければ None。失敗も None。"""
    try:
        root = base or sample_dir()
        if not root.is_dir():
            return None
        weeks = sorted(p.stem for p in root.glob("*.jsonl"))
        if not weeks:
            return None
        labels = root / LABELS_SUBDIR
        pending = [w for w in weeks if not (labels / f"{w}.jsonl").exists()]
        if not pending:
            return f"統合の止めた組の裁定: 未裁定 0 週 (抜き出し {len(weeks)} 週)"
        pairs = sum(_count_lines(root / f"{w}.jsonl") for w in pending)
        at = now or datetime.now(UTC)
        start = _week_start(pending[0])
        age_weeks = (at - start).days // 7 if start else 0
        mark = "⚠️" if len(pending) >= STALE_WEEKS else ""
        return (
            f"{mark}統合の止めた組の裁定: 未裁定 {len(pending)} 週・{pairs} 組"
            f" (最古 {pending[0]}、{age_weeks} 週前)"
        )
    except Exception as e:  # noqa: BLE001 — heartbeat 本体を止めない
        _log.warning("veto_sample_line_failed", error_type=type(e).__name__)
        return None


__all__ = [
    "STALE_WEEKS",
    "build_veto_sample_line",
    "run_weekly_veto_sample",
    "sample_dir",
    "write_weekly_sample",
]
