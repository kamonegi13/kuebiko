"""P0 収集飢餓修正に伴う dedup_seen_urls の last_seen touch と purge 基準のテスト。

source 層の未見選別 (seen 済み URL を pipeline に流さない) の導入で、既知 URL が
mark_url_seen を通らなくなる。「feed にまだ現れている」記録は filter_seen_and_touch が
last_seen を更新することで維持し、retention purge は last_seen 基準
(= feed から消えて N 日後に忘れる) に変更する。first_seen 基準のままだと、恒常掲載
URL の記憶が 90 日で消えて再投稿される (監査 2026-07-05 P5)。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.storage.run_history import RunHistoryRepository


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "dedup.db")


class TestFilterSeenAndTouch:
    def test_returns_seen_subset(self, repo: RunHistoryRepository) -> None:
        repo.mark_url_seen(url_hash="a" * 32, url="https://e/a", title="a")
        seen = repo.filter_seen_and_touch(["a" * 32, "b" * 32])
        assert seen == {"a" * 32}

    def test_touches_last_seen(self, repo: RunHistoryRepository) -> None:
        old = datetime.now(UTC) - timedelta(days=80)
        repo.mark_url_seen(url_hash="a" * 32, url="https://e/a", title="a", when=old)
        repo.filter_seen_and_touch(["a" * 32])
        with repo._connect() as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT first_seen, last_seen FROM dedup_seen_urls WHERE url_hash = ?",
                ("a" * 32,),
            ).fetchone()
        first = datetime.fromisoformat(str(row["first_seen"]))
        last = datetime.fromisoformat(str(row["last_seen"]))
        assert last > first
        assert (datetime.now(UTC) - last) < timedelta(minutes=1)

    def test_empty_input(self, repo: RunHistoryRepository) -> None:
        assert repo.filter_seen_and_touch([]) == set()

    def test_does_not_increment_seen_count(self, repo: RunHistoryRepository) -> None:
        # seen_count は pipeline 到達回数の指標なので touch では増やさない
        repo.mark_url_seen(url_hash="a" * 32, url="https://e/a", title="a")
        repo.filter_seen_and_touch(["a" * 32])
        with repo._connect() as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT seen_count FROM dedup_seen_urls WHERE url_hash = ?",
                ("a" * 32,),
            ).fetchone()
        assert int(row["seen_count"]) == 1


class TestPurgeByLastSeen:
    def test_entry_still_in_feed_survives_purge(self, repo: RunHistoryRepository) -> None:
        # first_seen は 100 日前でも、last_seen が新しければ purge されない
        old = datetime.now(UTC) - timedelta(days=100)
        repo.mark_url_seen(url_hash="a" * 32, url="https://e/a", title="a", when=old)
        repo.filter_seen_and_touch(["a" * 32])  # last_seen を現在に
        purged = repo.purge_old_dedup_entries(days=90)
        assert purged == 0
        assert repo.is_url_seen("a" * 32)

    def test_entry_gone_from_feeds_is_purged(self, repo: RunHistoryRepository) -> None:
        old = datetime.now(UTC) - timedelta(days=100)
        repo.mark_url_seen(url_hash="b" * 32, url="https://e/b", title="b", when=old)
        purged = repo.purge_old_dedup_entries(days=90)
        assert purged == 1
        assert not repo.is_url_seen("b" * 32)
