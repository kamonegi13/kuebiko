"""統合で止めた組の週次の抜き出し (2026-11-20、11-19 監査 D3 I-8)。

約束した週 10 組の抜き取りが 26 日で 0 回だった。抜き出しをジョブにし、未裁定の週を
ハートビートに出す。ここでは: 抜く対象 (hub_split_blocked を含む・relation_candidate は外す)、
同じ週の再実行で印を重ねないこと、未裁定の行の ⚠ の線を固定する。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import src.eventnews.veto_sampling as vs
import src.ui.services.eventnews_hourly_job as hourly
from src.eventnews.merge import VetoedLink
from src.storage.run_history import RunHistoryRepository
from src.ui.services.veto_sample_job import (
    STALE_WEEKS,
    build_veto_sample_line,
    write_weekly_sample,
)

NOW = datetime(2026, 11, 23, 22, 40, tzinfo=UTC)  # 月曜 07:40 JST (ISO 週 2026-W48)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RunHistoryRepository:
    r = RunHistoryRepository(db_path=tmp_path / "t.db")
    vetoes = [
        VetoedLink("ev-a", "ev-b", "conflict", "victim differs", 1, True),  # blocked
        VetoedLink("ev-c", "ev-d", "hub_split", "advisory hub", 2, True),  # hub_split_blocked
        VetoedLink("ev-e", "ev-f", "lineage", "patch -> exploit", 1, False),  # 関係の候補
    ]
    r.record_merge_vetoes(vetoes, seen_at=NOW, mode="1")

    def _describe(_repo: Any, item_id: str, _counts: Any) -> dict[str, Any]:
        return {"item_id": item_id, "article_count": 1, "titles": ["t"], "focus": {}}

    monkeypatch.setattr(vs, "_describe_item", _describe)
    monkeypatch.setattr(hourly, "_entity_counts", lambda *_a, **_k: {})
    return r


def test_samples_blocked_and_hub_split_but_not_relation_candidates(
    repo: RunHistoryRepository, tmp_path: Path
) -> None:
    out = tmp_path / "samples"

    result = write_weekly_sample(repo, now=NOW, out_dir=out)

    assert result["week"] == "2026-W48"
    assert result["picked"] == 2
    rows = [json.loads(x) for x in (out / "2026-W48.jsonl").read_text().splitlines()]
    assert {r["action"] for r in rows} == {"blocked", "hub_split_blocked"}
    remaining = repo.list_merge_vetoes(since=NOW, unsampled_only=True)
    assert [(r.item_a, r.action) for r in remaining] == [("ev-e", "relation_candidate")]


def test_rerun_in_same_week_does_nothing(repo: RunHistoryRepository, tmp_path: Path) -> None:
    out = tmp_path / "samples"
    write_weekly_sample(repo, now=NOW, out_dir=out)
    before = (out / "2026-W48.jsonl").read_text()

    result = write_weekly_sample(repo, now=NOW, out_dir=out)

    assert result == {"week": "2026-W48", "skipped": "exists"}
    assert (out / "2026-W48.jsonl").read_text() == before


def test_empty_pool_writes_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = RunHistoryRepository(db_path=tmp_path / "e.db")
    out = tmp_path / "samples"

    result = write_weekly_sample(r, now=NOW, out_dir=out)

    assert result == {"week": "2026-W48", "picked": 0, "pool": 0}
    assert not out.exists()


def _write_week(base: Path, week: str, pairs: int, *, labeled: bool) -> None:
    base.mkdir(parents=True, exist_ok=True)
    (base / f"{week}.jsonl").write_text("{}\n" * pairs)
    if labeled:
        (base / "labels").mkdir(exist_ok=True)
        (base / "labels" / f"{week}.jsonl").write_text("{}\n")


def test_line_is_none_before_first_sample(tmp_path: Path) -> None:
    assert build_veto_sample_line(now=NOW, base=tmp_path / "missing") is None


def test_line_counts_pending_weeks(tmp_path: Path) -> None:
    _write_week(tmp_path, "2026-W47", 10, labeled=True)
    _write_week(tmp_path, "2026-W48", 10, labeled=False)

    line = build_veto_sample_line(now=NOW, base=tmp_path)

    assert line == "統合の止めた組の裁定: 未裁定 1 週・10 組 (最古 2026-W48、0 週前)"


def test_line_warns_when_judging_stalls(tmp_path: Path) -> None:
    for week in range(48 - STALE_WEEKS + 1, 49):
        _write_week(tmp_path, f"2026-W{week}", 10, labeled=False)

    line = build_veto_sample_line(now=NOW, base=tmp_path)

    assert line is not None and line.startswith("⚠️")
    assert f"未裁定 {STALE_WEEKS} 週" in line


def test_all_labeled_is_quiet(tmp_path: Path) -> None:
    _write_week(tmp_path, "2026-W48", 10, labeled=True)

    assert build_veto_sample_line(now=NOW, base=tmp_path) == (
        "統合の止めた組の裁定: 未裁定 0 週 (抜き出し 1 週)"
    )
