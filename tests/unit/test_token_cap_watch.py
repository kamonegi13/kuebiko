"""出力上限に当たった生成物の見張り・総括の必須の節の欠け (2026-11-20、11-19 監査 D1)。

週次の状況総括が上限で後ろの節を失ったまま 9 週投稿されていた。``llm_usage.hit_token_cap`` は
記録されていたが誰も見ていなかった。ハートビートの行の ⚠ の線と、節の欠けの記録を固定する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from src.storage.run_history import RunHistoryRepository
from src.synthesis.grounded.render import _log_missing_sections, _WireSections
from src.ui.services.token_cap_watch import build_token_cap_line

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def _seed(repo: RunHistoryRepository, rows: list[tuple[str, int, int]]) -> None:
    ts = (NOW - timedelta(hours=1)).isoformat()
    old = (NOW - timedelta(days=3)).isoformat()
    with repo._connect() as conn:  # noqa: SLF001
        for step, calls, caps in rows:
            for i in range(calls):
                conn.execute(
                    "INSERT INTO llm_usage (ts, provider, model, step, hit_token_cap)"
                    " VALUES (?, 'ollama', 'm', ?, ?)",
                    (ts, step, 1 if i < caps else 0),
                )
        # 窓の外の到達は数えない
        conn.execute(
            "INSERT INTO llm_usage (ts, provider, model, step, hit_token_cap)"
            " VALUES (?, 'ollama', 'm', 'synthesis_narrative', 1)",
            (old,),
        )


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "t.db")


def test_no_hits_is_quiet(repo: RunHistoryRepository) -> None:
    _seed(repo, [("event_news", 50, 0), ("article_summary", 10, 5)])

    assert build_token_cap_line(repo, now=NOW) == "出力上限の到達 (24h): なし"


def test_a_single_synthesis_hit_warns(repo: RunHistoryRepository) -> None:
    _seed(repo, [("synthesis_narrative", 2, 1)])

    assert build_token_cap_line(repo, now=NOW) == "⚠️出力上限の到達 (24h): 状況総括 1/2"


def test_rare_event_news_hit_is_listed_without_warning(repo: RunHistoryRepository) -> None:
    _seed(repo, [("event_news", 120, 1)])

    assert build_token_cap_line(repo, now=NOW) == "出力上限の到達 (24h): 事象ニュース 1/120"


def test_frequent_hits_warn(repo: RunHistoryRepository) -> None:
    _seed(repo, [("digest_deep_dive", 4, 2)])

    line = build_token_cap_line(repo, now=NOW)

    assert line is not None and line.startswith("⚠️")


def test_missing_required_sections_are_logged() -> None:
    sections = _WireSections(headline="見出し", weight_section="比重の節")

    with capture_logs() as logs:
        _log_missing_sections(sections, period_label="2026-W47")

    assert [(x["event"], x["missing"]) for x in logs] == [
        ("synthesis_sections_missing", ["pir_section"])
    ]


def test_complete_sections_log_nothing() -> None:
    sections = _WireSections(headline="h", weight_section="w", pir_section="p")

    with capture_logs() as logs:
        _log_missing_sections(sections, period_label="d")

    assert logs == []
