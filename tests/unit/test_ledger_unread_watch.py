"""台帳の未読の根拠の見張り (2026-11-20、11-19 監査 D2 P1-9)。

監査は ``assessed_at IS NULL`` を「未評価の滞留 2,906 行」と読んだが、assessed_at は ACH が
**引用した**根拠にだけ付く。読まれていない根拠は ``read_at IS NULL`` で、実数は 13 行だった。
正しい指標 (active の状況の未読・weak を除く) をハートビートに出す。閾値は事前登録:
未読 50 件超 または 最古 12 時間超で ⚠。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.storage.run_history import RunHistoryRepository
from src.ui.services.ledger_unread_watch import build_ledger_unread_line

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def _seed(repo: RunHistoryRepository, rows: list[tuple[str, str, str | None]]) -> None:
    with repo._connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO situations (situation_id, title, status, opened_at, last_evidence_at)"
            " VALUES ('s1', 't', 'active', ?, ?)",
            (NOW.isoformat(), NOW.isoformat()),
        )
        for aid, added, read in rows:
            conn.execute(
                "INSERT INTO situation_evidence (situation_id, article_id, polarity, added_at,"
                " read_at) VALUES ('s1', ?, 'support', ?, ?)",
                (aid, added, read),
            )


def test_no_unread_is_fine(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "r.db")
    _seed(repo, [("a", NOW.isoformat(), NOW.isoformat())])
    assert build_ledger_unread_line(repo, now=NOW) == "台帳の未読の根拠: 0 件"


def test_old_unread_warns(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "r.db")
    old = (NOW - timedelta(hours=13)).isoformat()
    _seed(repo, [("a", old, None), ("b", NOW.isoformat(), None)])
    line = build_ledger_unread_line(repo, now=NOW)
    assert line is not None and line.startswith("⚠️") and "2 件" in line and "13h" in line


def test_recent_few_unread_is_not_a_warning(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "r.db")
    _seed(repo, [("a", (NOW - timedelta(hours=2)).isoformat(), None)])
    line = build_ledger_unread_line(repo, now=NOW)
    assert line is not None and "⚠️" not in line and "1 件" in line
