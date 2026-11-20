"""切替の前後で出力の分布を比べる見張り (2026-11-20、11-19 監査 D4 P1-5 (b))。

11-17 の s26r 切替で要約の low 率が 24% → 39% にずれたのに 2 日気付かなかった。事象の束ね方・
feed の構成をそろえた low 率・⚠ の線・出す日を固定する。
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.storage.records import ArticleRecord, RunRecord
from src.storage.run_history import RunHistoryRepository
from src.ui.services.output_drift_watch import (
    ChangeEvent,
    Row,
    build_output_drift_line,
    cluster_events,
    window_stats,
)

T0 = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)


def test_events_within_six_hours_are_one_change() -> None:
    events = [
        ChangeEvent(T0, "model_tiers 版58"),
        ChangeEvent(T0 + timedelta(hours=3), "model_tiers 版59"),
        ChangeEvent(T0 + timedelta(days=1), "旗の変更"),
    ]

    out = cluster_events(events)

    assert [e.label for e in out] == ["model_tiers 版58・model_tiers 版59", "旗の変更"]


def test_low_rate_is_standardized_to_the_earlier_feed_mix() -> None:
    # 後の窓で low の多い feed B が増えただけ (feed ごとの low 率は変わらない)
    rows = [Row("A", "medium", 100)] * 9 + [Row("A", "low", 100)] + [Row("B", "low", 100)] * 10

    stats = window_stats(rows, feed_weights=Counter({"A": 9, "B": 1}))

    assert round(stats.low, 2) == 0.55  # 素の low 率は上がって見える
    assert round(stats.low_std, 2) == 0.19  # 前の構成にそろえると 0.9*0.1 + 0.1*1.0


def _seed(repo: RunHistoryRepository, at: datetime, importance: str, n: int, feed: str) -> None:
    rid = repo.start_run(RunRecord(started_at=at, pipeline="t", dry_run=False))
    for i in range(n):
        repo.add_article(
            ArticleRecord(
                run_id=rid,
                article_id=f"{feed}-{at.isoformat()}-{importance}-{i}",
                title="t",
                url=f"https://example.com/{feed}/{at.timestamp()}/{importance}/{i}",
                status="posted",
                importance=importance,
                summary="要約" * 100,
                feed_title=feed,
                created_at=at,
            )
        )


def _repo_with_switch(tmp_path: Path, *, low_after: int) -> RunHistoryRepository:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    _seed(repo, T0 - timedelta(days=1), "low", 20, "F")
    _seed(repo, T0 - timedelta(days=1), "medium", 60, "F")
    _seed(repo, T0 + timedelta(days=1), "low", low_after, "F")
    _seed(repo, T0 + timedelta(days=1), "medium", 80 - low_after, "F")
    with repo._connect() as conn:  # noqa: SLF001
        # 本番 (PG) では config_store が作る表。SQLite の repo は持たないのでテストで作る
        conn.execute(
            "CREATE TABLE IF NOT EXISTS app_config_versions (config_key TEXT, value_json TEXT,"
            " version INTEGER, note TEXT, created_at TEXT)"
        )
        conn.execute(
            "INSERT INTO app_config_versions (config_key, value_json, version, note, created_at)"
            " VALUES ('model_tiers', '{}', 59, '', ?)",
            (T0.isoformat(),),
        )
    return repo


def test_a_shift_after_a_switch_warns_the_day_after_the_window_closes(tmp_path: Path) -> None:
    repo = _repo_with_switch(tmp_path, low_after=40)

    line = build_output_drift_line(repo, now=T0 + timedelta(days=3, hours=12))

    assert line is not None and line.startswith("⚠️切替の前後 11-17 model_tiers 版59")
    assert "low 25%→50%" in line


def test_no_shift_still_reports_no_change(tmp_path: Path) -> None:
    repo = _repo_with_switch(tmp_path, low_after=20)

    line = build_output_drift_line(repo, now=T0 + timedelta(days=3, hours=12))

    assert line is not None and "変化なし" in line and not line.startswith("⚠")


def test_nothing_is_reported_before_the_window_closes(tmp_path: Path) -> None:
    repo = _repo_with_switch(tmp_path, low_after=40)

    assert build_output_drift_line(repo, now=T0 + timedelta(days=2)) is None
