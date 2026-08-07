"""feed 死活検知 + 日次 heartbeat のテスト (監査 2026-07-05 P2)。

「エラーにならない失敗」(feed の恒常無産出) は run 成否から見えない。
CISA KEV advisories が 6 週間無音でも全 run succeeded だった実測病理を受け、
per-feed fetch 結果の永続化と沈黙 feed 検出を回帰固定する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.storage.run_history import RunHistoryRepository, SourceFetchHealth
from src.ui.services.source_health import (
    SilentFeed,
    build_heartbeat_text,
    detect_silent_feeds,
)

_NOW = datetime(2026, 7, 5, 8, 0, tzinfo=UTC)


def _health(url: str, *, failures: int = 0, entries: int = 10) -> SourceFetchHealth:
    return SourceFetchHealth(
        source_key=url,
        name="x",
        last_ok_at=_NOW if failures == 0 else None,
        last_error_at=_NOW if failures else None,
        last_error="http: 404" if failures else None,
        consecutive_failures=failures,
        last_article_count=entries,
        updated_at=_NOW,
    )


class TestDetectSilentFeeds:
    def test_recent_yield_is_not_silent(self) -> None:
        silent = detect_silent_feeds(
            [("Feed A", "https://a/feed")],
            {"https://a/feed": _NOW - timedelta(days=2)},
            {},
            now=_NOW,
        )
        assert silent == []

    def test_stale_yield_is_silent_no_yield(self) -> None:
        # 15 日以上産出が無い enabled feed は沈黙扱い (CISA 病理の検出)
        silent = detect_silent_feeds(
            [("CISA", "https://cisa/feed")],
            {"https://cisa/feed": _NOW - timedelta(days=45)},
            {"https://cisa/feed": _health("https://cisa/feed")},
            now=_NOW,
        )
        assert len(silent) == 1
        assert silent[0].kind == "no_yield"

    def test_consecutive_fetch_failures_classified_as_fetch_error(self) -> None:
        silent = detect_silent_feeds(
            [("Dead", "https://dead/feed")],
            {"https://dead/feed": _NOW - timedelta(days=20)},
            {"https://dead/feed": _health("https://dead/feed", failures=30)},
            now=_NOW,
        )
        assert silent[0].kind == "fetch_error"

    def test_never_yielded_but_parses_zero_entries_is_no_entries(self) -> None:
        # fetch は成功するが entry 0 件 = parse 異常/空 feed の疑い
        silent = detect_silent_feeds(
            [("Empty", "https://empty/feed")],
            {},
            {"https://empty/feed": _health("https://empty/feed", entries=0)},
            now=_NOW,
        )
        assert silent[0].kind == "no_entries"

    def test_sorted_by_name(self) -> None:
        old = _NOW - timedelta(days=30)
        silent = detect_silent_feeds(
            [("B feed", "https://b/f"), ("A feed", "https://a/f")],
            {"https://b/f": old, "https://a/f": old},
            {},
            now=_NOW,
        )
        assert [s.name for s in silent] == ["A feed", "B feed"]


class TestBuildHeartbeatText:
    def test_healthy_day(self) -> None:
        title, body, importance = build_heartbeat_text(
            run_counts={"succeeded": 40},
            silent=[],
            feeds_total=128,
        )
        assert "💓" in title
        assert "✅40" in body
        assert importance == "low"

    def test_silent_feeds_escalate_importance(self) -> None:
        silent = [
            SilentFeed(
                name="CISA",
                url="https://cisa/feed",
                kind="no_yield",
                last_article_at=None,
                consecutive_failures=0,
            )
        ]
        title, body, importance = build_heartbeat_text(
            run_counts={"succeeded": 40, "failed": 2},
            silent=silent,
            feeds_total=128,
        )
        assert "CISA" in body
        assert importance == "medium"


class TestSourceFetchHealthRepo:
    @pytest.fixture
    def repo(self, tmp_path: Path) -> RunHistoryRepository:
        return RunHistoryRepository(db_path=tmp_path / "health.db")

    def test_ok_upsert_resets_failures(self, repo: RunHistoryRepository) -> None:
        repo.upsert_source_fetch_health([("https://a/f", "A", False, "http: 500", 0)])
        repo.upsert_source_fetch_health([("https://a/f", "A", False, "http: 500", 0)])
        rows = repo.list_source_fetch_health()
        assert rows[0].consecutive_failures == 2
        repo.upsert_source_fetch_health([("https://a/f", "A", True, "", 30)])
        rows = repo.list_source_fetch_health()
        assert rows[0].consecutive_failures == 0
        assert rows[0].last_article_count == 30
        assert rows[0].last_ok_at is not None
        # 直近エラーの痕跡は残す (いつから回復したかの手掛かり)
        assert rows[0].last_error_at is not None

    def test_multiple_feeds(self, repo: RunHistoryRepository) -> None:
        repo.upsert_source_fetch_health(
            [
                ("https://a/f", "A", True, "", 10),
                ("https://b/f", "B", False, "parse: bozo", 0),
            ]
        )
        rows = {r.source_key: r for r in repo.list_source_fetch_health()}
        assert rows["https://a/f"].consecutive_failures == 0
        assert rows["https://b/f"].consecutive_failures == 1
        assert rows["https://b/f"].last_error == "parse: bozo"

    def test_count_runs_by_status_since(self, repo: RunHistoryRepository) -> None:
        from src.storage.run_history import RunRecord

        rid = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
        repo.finish_run(
            rid,
            status="succeeded",
            finished_at=datetime.now(UTC),
            total_fetched=1,
            summarized=1,
            posted=1,
            marked_read=0,
            error_count=0,
        )
        counts = repo.count_runs_by_status_since(hours=24)
        assert counts.get("succeeded") == 1


class TestHeartbeatNewLines:
    """製品鮮度 dead-man + アクター提案滞留 (監査 2026-07-16)。"""

    def test_products_and_proposals_lines_are_included(self) -> None:
        from src.ui.services.source_health import build_heartbeat_text

        title, body, importance = build_heartbeat_text(
            run_counts={"succeeded": 5},
            silent=[],
            feeds_total=100,
            products_line="製品鮮度: 週次総括 3d / recap 4d / spotlight 4d / 月次総括 9d",
            proposals_line="アクター提案 pending 3 件 (最古 2d、要レビュー)",
        )
        assert "製品鮮度" in body
        assert "アクター提案" in body
        assert importance == "low"

    def test_warn_in_products_line_escalates_importance(self) -> None:
        from src.ui.services.source_health import build_heartbeat_text

        _t, body, importance = build_heartbeat_text(
            run_counts={"succeeded": 5},
            silent=[],
            feeds_total=100,
            products_line="製品鮮度: ⚠️週次総括 10d / recap 4d",
        )
        assert "⚠️週次総括" in body
        assert importance == "medium"

    def test_warn_in_proposals_line_escalates_importance(self) -> None:
        from src.ui.services.source_health import build_heartbeat_text

        _t, _body, importance = build_heartbeat_text(
            run_counts={"succeeded": 5},
            silent=[],
            feeds_total=100,
            proposals_line="⚠️アクター提案 pending 33 件 (最古 23d、要レビュー)",
        )
        assert importance == "medium"
