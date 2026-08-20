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
    # 40 日前の run_logs を仕込む (既定 30 日 retention 対象)
    old = datetime.now(UTC) - timedelta(days=40)
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
async def test_daily_maintenance_purges_old_article_bodies(tmp_path: Path) -> None:
    """監査 backlog 2026-07-05: body 90d retention (schema 記載のみで未実装だった)。"""
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
