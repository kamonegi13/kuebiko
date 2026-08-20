"""概観 (ダッシュボード) actor-nation 集計の母集団反転テスト (2026-08-21)。

`_actor_nation_counts` が articles.subject_actor_ids (主題評価済み) 起点で母集団を作る
ことを検証する。旧実装は article_entities の言及 (mention) を数えており、国家情勢ボード
の攻撃者面 (75c7dee, 2026-08-20) / 地図 flow (同日) と同じ欠陥 (記事の主題でないアクター
の混入) があった。本テストはその反転の回帰防止 (tests/unit/test_situation_attacker_face.py
と同型)。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord
from src.ui.services.overview import _actor_nation_counts, _connect


def _start_run(repo: RunHistoryRepository) -> int:
    return repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=True))


def test_mention_only_article_excluded_from_actor_nation_counts(tmp_path: Path) -> None:
    """言及のみ (評価済みだが主題なし) の記事は国籍別件数に出ない (今回のバグの fixture 化)。"""
    db = tmp_path / "nat_mention_only.db"
    repo = RunHistoryRepository(db_path=db)
    rid = _start_run(repo)
    now = datetime.now(UTC)
    repo.add_article(
        ArticleRecord(
            run_id=rid,
            article_id="mo1",
            title="mo1",
            url="https://e/mo1",
            status="posted",
            created_at=now,
            subject_actor_ids="",
            subject_actor_source="none",
        )
    )
    repo.add_article_entities("mo1", [("actor", "volt_typhoon")], when=now)

    con = _connect(db)
    try:
        counts = _actor_nation_counts(
            con, now - timedelta(days=1), now + timedelta(days=1), {"volt_typhoon": "cn"}
        )
    finally:
        con.close()
    assert counts == {}


def test_subject_actor_article_counted_in_actor_nation_counts(tmp_path: Path) -> None:
    """subject_actor_ids に該当する actor は国籍別件数に数えられる。"""
    db = tmp_path / "nat_subject_match.db"
    repo = RunHistoryRepository(db_path=db)
    rid = _start_run(repo)
    now = datetime.now(UTC)
    repo.add_article(
        ArticleRecord(
            run_id=rid,
            article_id="s1",
            title="s1",
            url="https://e/s1",
            status="posted",
            created_at=now,
            subject_actor_ids="volt_typhoon",
            subject_actor_source="llm",
        )
    )

    con = _connect(db)
    try:
        counts = _actor_nation_counts(
            con, now - timedelta(days=1), now + timedelta(days=1), {"volt_typhoon": "cn"}
        )
    finally:
        con.close()
    assert counts == {"cn": 1}
