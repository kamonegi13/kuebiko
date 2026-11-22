"""近さの経路 (頻出の名前 + 時刻の近さ) で統合した組の週次の抜き出し (2026-11-21)。

事前登録 docs/replay_prereg/merge_frequent_recent.md の運用の約束: この経路の統合を週 10 組抜いて
Opus で裁定し、0.9 を割り続けるなら ``EVENTNEWS_MERGE_FREQUENT_RECENT`` を shadow に戻す。
抜き出し (LLM なし) だけを週次ジョブ ``weekly-veto-sample`` に相乗りさせ、裁定は手作業
(``data/eval/cairn_split_1121/tools/label_pairs.py`` と同じ指示 v2)。

- 対象: 直近 7 日の毎時の統合の判断 (取り消し済みを除く) のうち、辺の出どころが frequent かつ
  cos < 0.80 の辺を持つもの。判断 1 件につき確率が最大の辺 1 本。
- 書き先: ``data/eval/merge_recent_samples/<YYYY-Www>.jsonl`` (ローカルのみ・gitignore)。
  同じ週のファイルが既にあれば何もしない。
"""

from __future__ import annotations

import json
import os
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.eventnews.veto_sampling import week_label
from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)

SAMPLE_DIR_ENV = "RECENT_MERGE_SAMPLE_DIR"
DEFAULT_SAMPLE_DIR = "/app/data/eval/merge_recent_samples"
SAMPLE_SIZE = 10
WINDOW_DAYS = 7
#: 近さの経路の辺 = 頻出の名前の辺のうち、従来の救済の下限 (0.80) 未満の cos
RECENT_COS_BELOW = 0.80

_SQL = (
    "SELECT id, target_id, absorbed_id, edges_json FROM event_merge_decisions"
    " WHERE run_kind = 'hourly' AND undone_at IS NULL AND decided_at >= ? ORDER BY id"
)
_SQL_TITLES = (
    "SELECT article_id, COALESCE(title, original_title, '') AS title FROM articles"
    " WHERE article_id IN ({placeholders})"
)


def sample_dir() -> Path:
    return Path(os.environ.get(SAMPLE_DIR_ENV, DEFAULT_SAMPLE_DIR))


def recent_edge(edges_json: str) -> dict[str, Any] | None:
    """判断の辺のうち近さの経路の辺で確率が最大のもの (無ければ None、純粋関数)。"""
    try:
        edges = json.loads(edges_json or "[]")
    except ValueError:
        return None
    recent = [
        e
        for e in edges
        if isinstance(e, dict)
        and e.get("source") == "frequent"
        and float(e.get("cos") or 1.0) < RECENT_COS_BELOW
    ]
    if not recent:
        return None
    return max(recent, key=lambda e: float(e.get("proba") or 0.0))


def pick(
    rows: list[dict[str, Any]], *, label: str, size: int = SAMPLE_SIZE
) -> list[dict[str, Any]]:
    """候補から週ごとに決まった乱数で ``size`` 件 (純粋関数)。"""
    cand = []
    for r in rows:
        e = recent_edge(str(r["edges_json"]))
        if e is None:
            continue
        cand.append(
            {
                "decision_id": r["id"],
                "target": r["target_id"],
                "absorbed": r["absorbed_id"],
                "a": e.get("a"),
                "b": e.get("b"),
                "p": e.get("proba"),
                "cos": e.get("cos"),
            }
        )
    rng = random.Random(label)
    return sorted(rng.sample(cand, min(size, len(cand))), key=lambda c: c["decision_id"])


def _titles(repo: RunHistoryRepository, ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    with repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            _SQL_TITLES.format(placeholders=",".join("?" for _ in ids)),  # noqa: S608
            ids,
        ).fetchall()
    return {str(r["article_id"]): str(r["title"] or "") for r in rows}


def write_weekly_recent_sample(
    repo: RunHistoryRepository, *, now: datetime, out_dir: Path
) -> dict[str, Any]:
    """1 週分を書く。同期 (呼び手がスレッドへ逃がす)。"""
    label = week_label(now)
    path = out_dir / f"{label}.jsonl"
    if path.exists():
        return {"week": label, "skipped": "exists"}
    since = (now - timedelta(days=WINDOW_DAYS)).astimezone(UTC).isoformat()
    with repo._connect() as conn:  # noqa: SLF001
        rows = [dict(r) for r in conn.execute(_SQL, (since,)).fetchall()]
    picked = pick(rows, label=label)
    if not picked:
        return {"week": label, "picked": 0, "pool": len(rows)}
    titles = _titles(repo, sorted({x for p in picked for x in (p["a"], p["b"]) if x}))
    lines = [
        json.dumps(
            {**p, "ta": titles.get(p["a"], ""), "tb": titles.get(p["b"], "")}, ensure_ascii=False
        )
        for p in picked
    ]
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)
    return {"week": label, "picked": len(picked), "pool": len(rows)}


__all__ = ["pick", "recent_edge", "sample_dir", "write_weekly_recent_sample"]
