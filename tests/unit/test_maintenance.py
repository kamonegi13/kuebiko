"""日次 DB 衛生バッチのテスト (監査 2026-07-05)。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.storage.run_history import RunHistoryRepository, RunRecord


@pytest.mark.asyncio
async def test_daily_maintenance_purges_old_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.ui.services import maintenance

    db = tmp_path / "maint.db"
    repo = RunHistoryRepository(db_path=db)
    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
    # 200 日前の run_logs を仕込む (既定 180 日 retention 対象)
    old = datetime.now(UTC) - timedelta(days=200)
    with repo._connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO run_logs (run_id, seq, ts, stream, line) VALUES (?,?,?,?,?)",
            (run_id, 1, old.isoformat(), "stdout", "old line"),
        )

    await maintenance.run_daily_maintenance(repo=repo)

    with repo._connect() as conn:  # noqa: SLF001
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM run_logs WHERE run_id=?", (run_id,)
        ).fetchone()
    assert remaining["n"] == 0


@pytest.mark.asyncio
async def test_daily_maintenance_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.ui.services import maintenance

    class _BoomRepo:
        def purge_old_logs(self) -> int:
            raise RuntimeError("db down")

    # purge が例外を投げても run_daily_maintenance は飲む
    await maintenance.run_daily_maintenance(repo=_BoomRepo())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_daily_maintenance_purges_old_article_bodies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """監査 backlog 2026-07-05: body 90d retention。2026-10-29 から旗 BODY_PURGE_DAYS=90 の経路。"""
    monkeypatch.setenv("BODY_PURGE_DAYS", "90")
    from src.storage.run_history import ArticleRecord
    from src.ui.services import maintenance

    db = tmp_path / "body.db"
    repo = RunHistoryRepository(db_path=db)
    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
    # importance は low にする — high は D4 の GC-root (triage root) で purge 免除される
    # (root ごとの保護は test_body_retention_roots.py が検証)
    for aid in ("a-old", "a-new", "a-legacy-old"):
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id=aid,
                title=aid,
                url=f"https://example.com/{aid}",
                importance="low",
                status="posted",
            )
        )
    old_iso = (datetime.now(UTC) - timedelta(days=120)).isoformat()
    new_iso = datetime.now(UTC).isoformat()
    with repo._connect() as conn:  # noqa: SLF001
        # body_fetched_at 基準 (post 経路)
        conn.execute(
            "UPDATE articles SET body='OLD', body_fetched_at=? WHERE article_id='a-old'",
            (old_iso,),
        )
        conn.execute(
            "UPDATE articles SET body='NEW', body_fetched_at=? WHERE article_id='a-new'",
            (new_iso,),
        )
        # body_fetched_at NULL の旧経路は created_at 基準
        conn.execute(
            "UPDATE articles SET body='LEGACY', body_fetched_at=NULL, created_at=?"
            " WHERE article_id='a-legacy-old'",
            (old_iso,),
        )

    await maintenance.run_daily_maintenance(repo=repo)

    with repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute("SELECT article_id, body FROM articles ORDER BY article_id").fetchall()
    bodies = {r["article_id"]: r["body"] for r in rows}
    assert bodies["a-old"] is None  # 90d 超は NULL 化
    assert bodies["a-legacy-old"] is None  # created_at fallback でも NULL 化
    assert bodies["a-new"] == "NEW"  # 新しい body は保持


@pytest.mark.asyncio
async def test_daily_maintenance_purges_old_ops_notices(tmp_path: Path) -> None:
    """ops 通知の永続化 (2026-08-21): access_audit と同じ 180 日 retention を日次衛生で回す。"""
    from src.ui.services import maintenance

    db = tmp_path / "ops_notices.db"
    repo = RunHistoryRepository(db_path=db)
    now = datetime.now(UTC)
    repo.record_ops_notice(title="recent", body="b", importance="low", sent=True, when=now)
    repo.record_ops_notice(
        title="old", body="b", importance="low", sent=True, when=now - timedelta(days=200)
    )

    await maintenance.run_daily_maintenance(repo=repo)

    remaining = repo.list_ops_notices()
    assert [n["title"] for n in remaining] == ["recent"]


# ----- BODY_PURGE_DAYS (2026-10-29): 本文の purge を旗で止める -----

_COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"


def _repo_with_old_body(tmp_path: Path) -> RunHistoryRepository:
    from src.storage.run_history import ArticleRecord

    repo = RunHistoryRepository(db_path=tmp_path / "body.db")
    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=run_id,
            article_id="a1",
            title="t",
            url="https://example.com/a1",
            importance="low",
            status="posted",
        )
    )
    old = (datetime.now(UTC) - timedelta(days=200)).isoformat()
    with repo._connect() as conn:  # noqa: SLF001
        conn.execute(
            "UPDATE articles SET body='BODY', body_fetched_at=? WHERE article_id='a1'", (old,)
        )
    return repo


def _body_of(repo: RunHistoryRepository) -> str | None:
    with repo._connect() as conn:  # noqa: SLF001
        row = conn.execute("SELECT body FROM articles WHERE article_id='a1'").fetchone()
    return row["body"] if row else None


@pytest.mark.asyncio
async def test_body_purge_skipped_when_flag_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.ui.services import maintenance

    monkeypatch.delenv("BODY_PURGE_DAYS", raising=False)
    repo = _repo_with_old_body(tmp_path)

    await maintenance.run_daily_maintenance(repo=repo)

    assert _body_of(repo) == "BODY"


@pytest.mark.asyncio
async def test_body_purge_runs_with_flag_90(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.ui.services import maintenance

    monkeypatch.setenv("BODY_PURGE_DAYS", "90")
    repo = _repo_with_old_body(tmp_path)

    await maintenance.run_daily_maintenance(repo=repo)

    assert _body_of(repo) is None


@pytest.mark.parametrize("raw", ["", "0", "-5", "abc", "9.5"])
def test_body_purge_days_invalid_or_zero_means_disabled(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.ui.services import maintenance

    monkeypatch.setenv("BODY_PURGE_DAYS", raw)

    assert maintenance.body_purge_days() == 0


def test_body_purge_days_reads_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.ui.services import maintenance

    monkeypatch.setenv("BODY_PURGE_DAYS", "90")

    assert maintenance.body_purge_days() == 90


def test_compose_defaults_body_purge_off() -> None:
    assert "BODY_PURGE_DAYS: ${BODY_PURGE_DAYS:-0}" in _COMPOSE.read_text(encoding="utf-8")
