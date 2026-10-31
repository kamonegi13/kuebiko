"""薄い本文の関門の誤落ち回収: 選定ロジックと状態遷移 (2026-10-30)。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest

from src.pipeline.thin_body_recovery import (
    effective_until,
    passes_current_gate,
    select_candidates,
)
from src.storage.records import ANALYSIS_ONLY_THIN_BODY_RECOVERY, ArticleRecord, RunRecord
from src.storage.run_history import RunHistoryRepository

_REASON = "degenerate_body: thin_body"
# 本文 600 字以上に決まり文句 (Video) が 1 語あるだけの長い技術記事 = 誤落ちの典型
_LONG_BODY = (
    "The attacker pivoted through the VPN and dumped credentials. " * 30
) + "Watch the Video."
_STUB_BODY = "Listen to this article. Video. " * 3  # 本物の薄い本文 (残り 200 字未満)


def _repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=str(tmp_path / "r.db"))


def _add(
    repo: RunHistoryRepository,
    aid: str,
    body: str | None,
    *,
    reason: str = _REASON,
    status: Literal["extract_failed", "posted"] = "extract_failed",
) -> None:
    rid = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=rid,
            article_id=aid,
            title="t",
            url=f"https://ex.com/{aid}",
            status=status,
            failure_reason=reason,
        )
    )
    if body is not None:
        repo.update_article_body(aid, body, source="full_extract")


def _ids(repo: RunHistoryRepository, aid: str) -> list[int]:
    with repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute("SELECT id FROM articles WHERE article_id=? ORDER BY id DESC", (aid,))
        return [int(r["id"]) for r in rows.fetchall()]


def test_long_article_with_media_keyword_passes_current_gate() -> None:
    assert passes_current_gate(_LONG_BODY, "t", "rss:x") is True


def test_real_stub_body_is_still_rejected_by_current_gate() -> None:
    assert passes_current_gate(_STUB_BODY, "t", "rss:x") is False


def test_select_candidates_keeps_only_gate_passing_rows_newest_first() -> None:
    now = datetime.now(UTC)
    rows: list[dict[str, object]] = [
        {
            "article_id": "old",
            "title": "t",
            "body": _LONG_BODY,
            "created_at": now - timedelta(days=5),
        },
        {"article_id": "stub", "title": "t", "body": _STUB_BODY, "created_at": now},
        {
            "article_id": "new",
            "title": "t",
            "body": _LONG_BODY,
            "created_at": now - timedelta(days=1),
        },
    ]
    assert [c.article_id for c in select_candidates(rows)] == ["new", "old"]
    assert [c.article_id for c in select_candidates(rows, limit=1)] == ["new"]


def test_list_excludes_other_reasons_bodyless_rows_and_dedups_article_id(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _add(repo, "a", _LONG_BODY)
    _add(repo, "a", _LONG_BODY)  # 同一 article_id の 2 行目
    _add(repo, "probe", _LONG_BODY, reason="degenerate_body: thin_body_probe")
    _add(repo, "nobody", None)
    _add(repo, "ok", _LONG_BODY, status="posted")
    ids = [r["article_id"] for r in repo.list_thin_body_extract_failed()]
    assert ids == ["a"]


def test_promote_is_idempotent_and_keeps_created_at(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _add(repo, "a", _LONG_BODY)
    before = repo.get_article("a")
    assert before is not None
    assert (
        repo.promote_recovered_thin_body(_ids(repo, "a")[0], importance="medium", category="apt")
        == 1
    )
    after = repo.get_article("a")
    assert after is not None
    assert (after.status, after.posted_channel, after.importance) == ("posted", None, "medium")
    assert after.failure_reason == "analysis_only:thin_body_recovery"
    assert after.created_at == before.created_at
    assert (
        repo.promote_recovered_thin_body(_ids(repo, "a")[0], importance="high", category="x") == 0
    )
    assert repo.list_thin_body_extract_failed() == []


def test_promote_only_target_row_and_supersede_others(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _add(repo, "a", _LONG_BODY)
    _add(repo, "a", _LONG_BODY)
    newest, older = _ids(repo, "a")
    assert repo.promote_recovered_thin_body(newest, importance="low", category=None) == 1
    with repo._connect() as conn:  # noqa: SLF001
        st = {
            int(r["id"]): (r["status"], r["failure_reason"])
            for r in conn.execute("SELECT id, status, failure_reason FROM articles").fetchall()
        }
    assert st[newest][0] == "posted"
    assert st[older] == ("extract_failed", "superseded: thin_body_recovered")
    assert repo.list_thin_body_extract_failed() == []  # 次回の選定に出ない


def test_promote_sets_analysis_only_column_and_pir_match_flags_it(tmp_path: Path) -> None:
    from src.pir.evaluator import _load_posted_rows

    repo = _repo(tmp_path)
    _add(repo, "a", _LONG_BODY)
    repo.promote_recovered_thin_body(_ids(repo, "a")[0], importance="high", category="apt")
    rec = repo.get_article("a")
    assert rec is not None and rec.posted_channel is None
    assert rec.analysis_only == ANALYSIS_ONLY_THIN_BODY_RECOVERY == "thin_body_recovery"
    rows, _ = _load_posted_rows(repo=repo, lookback_hours=24)
    assert [(r["article_id"], bool(r["analysis_only"])) for r in rows] == [("a", True)]


def test_effective_until_takes_earlier_of_until_and_min_age() -> None:
    now = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    until = datetime(2026, 10, 30, 6, 0, tzinfo=UTC)
    assert effective_until(None, 0, now=now) is None
    assert effective_until(until, 0, now=now) == until
    assert effective_until(None, 48, now=now) == now - timedelta(hours=48)
    assert effective_until(until, 1, now=now) == until


def test_recover_one_promotes_target_row_with_mocked_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from types import SimpleNamespace
    from typing import Any

    from src.pipeline import thin_body_recovery as mod

    repo = _repo(tmp_path)
    _add(repo, "a", _LONG_BODY)
    reenriched: list[str] = []

    async def fake_summarize(*a: Any, **k: Any) -> Any:
        return SimpleNamespace(importance="medium", category="apt")

    monkeypatch.setattr(mod, "_summarize_and_build", fake_summarize)
    monkeypatch.setattr(
        mod, "reenrich_stored_article", lambda r, aid, rec, msg: reenriched.append(aid)
    )
    row = repo.list_thin_body_extract_failed()[0]
    ok = asyncio.run(mod.recover_one(repo, row, llm=None, template=None, enrichment=None))  # type: ignore[arg-type]
    assert ok is True and reenriched == ["a"]
    rec = repo.get_article("a")
    assert rec is not None and (rec.status, rec.importance, rec.posted_channel) == (
        "posted",
        "medium",
        None,
    )
    # 2 回目 (既に posted) は選定されない
    assert repo.list_thin_body_extract_failed() == []


def test_select_excludes_short_bodies_by_default_and_flag_includes_them() -> None:
    short = "A normal sentence about an incident with enough words. " * 6  # 600 字未満で関門は通る
    assert len(short) < 600 and passes_current_gate(short, "t", "rss:x")
    rows: list[dict[str, object]] = [{"article_id": "s", "title": "t", "body": short, "id": 1}]
    assert select_candidates(rows) == []
    assert [c.article_id for c in select_candidates(rows, min_body_chars=0)] == ["s"]


def test_list_until_is_exclusive_upper_bound(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _add(repo, "a", _LONG_BODY)
    now = datetime.now(UTC)
    assert repo.list_thin_body_extract_failed(until=now - timedelta(hours=1)) == []
    assert len(repo.list_thin_body_extract_failed(until=now + timedelta(hours=1))) == 1


_SHORT_ROW: dict[str, object] = {
    "article_id": "rss:s",
    "title": "t",
    "body": "A normal sentence about an incident with enough words. " * 6,
}


def _probe_cfg() -> object:
    from unittest.mock import MagicMock

    return MagicMock()


@pytest.mark.parametrize(
    ("p_content", "expected"),
    [(0.01, "thin_body_probe"), (0.9, None)],
)
def test_probe_stage_reuses_production_probe(
    monkeypatch: pytest.MonkeyPatch, p_content: float, expected: str | None
) -> None:
    import asyncio
    from unittest.mock import AsyncMock

    from src.pipeline.thin_body_recovery import probe_stage_reason

    monkeypatch.setenv("THIN_BODY_PROBE", "1")
    probe = AsyncMock(return_value={"Yes": p_content, "No": 1 - p_content})
    monkeypatch.setattr("src.pipeline.briefing.probe_choice", probe)
    got = asyncio.run(probe_stage_reason(_SHORT_ROW, _probe_cfg()))  # type: ignore[arg-type]
    assert got == expected
    probe.assert_awaited_once()


def test_probe_stage_skips_long_body_and_disabled_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    from unittest.mock import AsyncMock

    from src.pipeline.thin_body_recovery import probe_stage_reason

    probe = AsyncMock(return_value={"Yes": 0.0, "No": 1.0})
    monkeypatch.setattr("src.pipeline.briefing.probe_choice", probe)
    long_row = {**_SHORT_ROW, "body": _LONG_BODY}
    monkeypatch.setenv("THIN_BODY_PROBE", "1")
    assert asyncio.run(probe_stage_reason(long_row, _probe_cfg())) is None  # type: ignore[arg-type]
    monkeypatch.setenv("THIN_BODY_PROBE", "0")
    assert asyncio.run(probe_stage_reason(_SHORT_ROW, _probe_cfg())) is None  # type: ignore[arg-type]
    probe.assert_not_awaited()


def test_script_probe_only_reports_without_writing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import asyncio
    import importlib.util
    from unittest.mock import AsyncMock, MagicMock

    spec = importlib.util.spec_from_file_location(
        "recover_script",
        Path(__file__).resolve().parents[2] / "scripts/recover_thin_body_articles.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rows = [
        {**_SHORT_ROW, "article_id": "rss:keep", "id": 1},
        {**_SHORT_ROW, "article_id": "rss:drop", "id": 2},
    ]
    repo = MagicMock()
    repo.list_thin_body_extract_failed.return_value = rows
    monkeypatch.setattr(mod, "RunHistoryRepository", lambda: repo)
    monkeypatch.setattr(mod, "load_app_config", lambda: MagicMock())
    monkeypatch.setattr(
        mod,
        "probe_stage_reason",
        AsyncMock(side_effect=lambda row, cfg: "thin_body_probe" if row["id"] == 2 else None),
    )
    promote = AsyncMock()
    monkeypatch.setattr(mod, "recover_one", promote)

    rc = asyncio.run(mod._run(False, None, None, None, 0, True))

    out = capsys.readouterr().out
    assert rc == 0
    assert "通る 1 / 2 段目で落ちる 1" in out
    promote.assert_not_awaited()
    repo.promote_recovered_thin_body.assert_not_called()
