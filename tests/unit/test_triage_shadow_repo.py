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


def test_jp_fields_round_trip(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    row = TriageShadowRow(
        article_id="rss:1",
        url="https://a.example/1",
        title="日本企業への攻撃",
        feed_title="A",
        feed_url="https://a.example/feed",
        current_importance="low",
        current_kept=False,
        flat_importance="low",
        hint_fired=False,
        hint_reasons=(),
        new_kept=True,  # current_kept と食い違わせて disagreements に乗せる
        jp_prob=0.42,
        jp_ml_fired=True,
        jp_cascade=True,
        new_kept_v2=True,
    )
    repo.record_triage_shadow([row])

    rows = repo.list_triage_shadow_disagreements(days=7)

    assert len(rows) == 1
    got = rows[0]
    assert got.jp_prob == 0.42
    assert got.jp_ml_fired is True
    assert got.jp_cascade is True
    assert got.new_kept_v2 is True


def test_jp_fields_default_to_none(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    repo.record_triage_shadow([_row(1, current_kept=True, new_kept=False)])

    rows = repo.list_triage_shadow_disagreements(days=7)

    assert rows[0].jp_prob is None
    assert rows[0].jp_ml_fired is None
    assert rows[0].jp_cascade is None
    assert rows[0].new_kept_v2 is None


def test_summarize_v2_2x2_ignores_null_new_kept_v2(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")

    def _row_v2(i: int, *, current_kept: bool, new_kept_v2: bool | None) -> TriageShadowRow:
        return TriageShadowRow(
            article_id=f"rss:{i}",
            url=f"https://a.example/{i}",
            title=f"記事 {i}",
            feed_title="A",
            feed_url="https://a.example/feed",
            current_importance="high" if current_kept else "low",
            current_kept=current_kept,
            flat_importance="low",
            hint_fired=False,
            hint_reasons=(),
            new_kept=current_kept,
            new_kept_v2=new_kept_v2,
        )

    repo.record_triage_shadow(
        [
            _row_v2(1, current_kept=True, new_kept_v2=True),
            _row_v2(2, current_kept=True, new_kept_v2=False),
            _row_v2(3, current_kept=False, new_kept_v2=True),
            _row_v2(4, current_kept=False, new_kept_v2=False),
            _row_v2(5, current_kept=False, new_kept_v2=None),  # ML 未使用 → 集計から除外
        ]
    )

    summary = repo.summarize_triage_shadow_v2(days=7)

    assert summary.both_kept == 1
    assert summary.current_only == 1
    assert summary.new_only == 1
    assert summary.both_dropped == 1
    assert summary.total == 4  # None の 1 件は入らない


def test_summarize_v2_rescued_counts_flat_low_rescued_by_ml(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    rescued = TriageShadowRow(
        article_id="rss:1",
        url="https://a.example/1",
        title="rescued",
        feed_title="A",
        feed_url="https://a.example/feed",
        current_importance="low",
        current_kept=False,
        flat_importance="low",
        hint_fired=False,
        hint_reasons=(),
        new_kept=False,
        jp_prob=0.5,
        jp_ml_fired=True,
        jp_cascade=True,
        new_kept_v2=True,
    )
    not_rescued = TriageShadowRow(
        article_id="rss:2",
        url="https://a.example/2",
        title="not rescued (flat 既に medium)",
        feed_title="A",
        feed_url="https://a.example/feed",
        current_importance="low",
        current_kept=False,
        flat_importance="medium",
        hint_fired=False,
        hint_reasons=(),
        new_kept=True,
        jp_prob=0.5,
        jp_ml_fired=True,
        jp_cascade=True,
        new_kept_v2=True,
    )
    repo.record_triage_shadow([rescued, not_rescued])

    summary = repo.summarize_triage_shadow_v2(days=7)

    assert summary.rescued == 1  # flat_importance='low' かつ new_kept_v2=1 のみ
    # article_importance_v2 に行が無い (current_kept=False で分析に回っていない) ので unknown
    assert summary.rescued_label_unknown == 1


def test_purge_removes_old_rows(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    repo.record_triage_shadow([_row(1)])

    # created_at は record 時点の現在時刻なので、0 日保持で即時削除されることを確認する
    purged = repo.purge_triage_shadow(days=0)

    assert purged == 1
    assert repo.summarize_triage_shadow(days=7).total == 0
