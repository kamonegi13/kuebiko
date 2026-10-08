"""triage_shadow テーブルの読み書き (2026-10-08、M4)。"""

from __future__ import annotations

from pathlib import Path

from src.storage.repo_triage_shadow import TriageShadowRow
from src.storage.run_history import RunHistoryRepository


def _row(
    i: int,
    *,
    current_kept: bool = True,
    new_kept: bool = True,
    hint_reasons: tuple[str, ...] = (),
    title: str = "",
) -> TriageShadowRow:
    return TriageShadowRow(
        article_id=f"rss:{i}",
        url=f"https://a.example/{i}",
        title=title or f"記事 {i}",
        feed_title="A",
        feed_url="https://a.example/feed",
        current_importance="high" if current_kept else "low",
        current_kept=current_kept,
        flat_importance="medium" if new_kept else "low",
        hint_fired=bool(hint_reasons),
        hint_reasons=hint_reasons,
        new_kept=new_kept,
    )


def test_record_and_summarize_2x2(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    repo.record_triage_shadow(
        [
            _row(1, current_kept=True, new_kept=True),
            _row(2, current_kept=True, new_kept=False),
            _row(3, current_kept=False, new_kept=True),
            _row(4, current_kept=False, new_kept=False),
        ]
    )

    summary = repo.summarize_triage_shadow(days=7)

    assert summary.both_kept == 1
    assert summary.current_only == 1
    assert summary.new_only == 1
    assert summary.both_dropped == 1
    assert summary.total == 4


def test_record_empty_is_noop(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    assert repo.record_triage_shadow([]) == 0
    assert repo.summarize_triage_shadow(days=7).total == 0


def test_disagreements_lists_only_mismatches(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    repo.record_triage_shadow(
        [
            _row(1, current_kept=True, new_kept=True, title="一致"),
            _row(2, current_kept=True, new_kept=False, title="日本関連の落選候補"),
            _row(3, current_kept=False, new_kept=True, title="新ルールで拾った記事"),
        ]
    )

    rows = repo.list_triage_shadow_disagreements(days=7)

    assert {r.article_id for r in rows} == {"rss:2", "rss:3"}


def test_hint_reasons_round_trip(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    repo.record_triage_shadow(
        [_row(1, current_kept=True, new_kept=False, hint_reasons=("jp", "nation:CN"))]
    )

    rows = repo.list_triage_shadow_disagreements(days=7)

    assert rows[0].hint_reasons == ("jp", "nation:CN")


def test_purge_removes_old_rows(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    repo.record_triage_shadow([_row(1)])

    # created_at は record 時点の現在時刻なので、0 日保持で即時削除されることを確認する
    purged = repo.purge_triage_shadow(days=0)

    assert purged == 1
    assert repo.summarize_triage_shadow(days=7).total == 0
