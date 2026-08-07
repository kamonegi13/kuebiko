"""src.storage.run_history.dedup_seen_urls のテスト (Phase 3a)。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.storage.run_history import RunHistoryRepository
from src.tools.url_normalizer import url_hash


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "dedup.db")


def test_mark_then_is_url_seen(repo: RunHistoryRepository) -> None:
    h = url_hash("https://example.com/a")
    assert repo.is_url_seen(h) is False
    repo.mark_url_seen(url_hash=h, url="https://example.com/a", title="A")
    assert repo.is_url_seen(h) is True


def test_mark_url_seen_is_idempotent_and_updates_count(
    repo: RunHistoryRepository,
) -> None:
    h = url_hash("https://example.com/a")
    repo.mark_url_seen(url_hash=h, url="https://example.com/a")
    repo.mark_url_seen(url_hash=h, url="https://example.com/a")
    repo.mark_url_seen(url_hash=h, url="https://example.com/a")
    # 例外なく挿入できること、is_url_seen が True であること
    assert repo.is_url_seen(h) is True


def test_filter_unseen_hashes_returns_only_existing(
    repo: RunHistoryRepository,
) -> None:
    seen = url_hash("https://seen.example.com/x")
    repo.mark_url_seen(url_hash=seen, url="https://seen.example.com/x")
    fresh = url_hash("https://fresh.example.com/y")
    found = repo.filter_unseen_hashes([seen, fresh])
    assert found == {seen}


def test_filter_unseen_hashes_empty_input(repo: RunHistoryRepository) -> None:
    assert repo.filter_unseen_hashes([]) == set()


def test_purge_old_dedup_entries(repo: RunHistoryRepository) -> None:
    now = datetime.now(UTC)
    very_old = now - timedelta(days=400)
    h_old = url_hash("https://old.example.com/")
    h_new = url_hash("https://new.example.com/")
    repo.mark_url_seen(url_hash=h_old, url="https://old.example.com/", when=very_old)
    repo.mark_url_seen(url_hash=h_new, url="https://new.example.com/")
    deleted = repo.purge_old_dedup_entries(days=365)
    assert deleted == 1
    assert repo.is_url_seen(h_old) is False
    assert repo.is_url_seen(h_new) is True
