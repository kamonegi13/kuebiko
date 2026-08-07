"""運用 config の DB store テスト (versioned, dual-backend は SQLite で検証)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.storage import config_store as cs


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "cfg.db"


def test_get_absent_returns_none(db: Path) -> None:
    assert cs.get_config("routing_rules", db_path=db) is None


def test_save_then_get_roundtrip(db: Path) -> None:
    value = [{"id": "R1", "channel": "alert", "when": {"flag": "kev"}}]
    v = cs.save_config("routing_rules", value, note="t", db_path=db)
    assert v == 1
    assert cs.get_config("routing_rules", db_path=db) == value


def test_versions_increment_and_current_is_latest(db: Path) -> None:
    cs.save_config("k", {"a": 1}, db_path=db)
    cs.save_config("k", {"a": 2}, db_path=db)
    v3 = cs.save_config("k", {"a": 3}, db_path=db)
    assert v3 == 3
    assert cs.get_config("k", db_path=db) == {"a": 3}
    # 旧 version も取得できる (revert 用)
    assert cs.get_config_version("k", 1, db_path=db) == {"a": 1}


def test_history_newest_first(db: Path) -> None:
    cs.save_config("k", {"a": 1}, note="first", db_path=db)
    cs.save_config("k", {"a": 2}, note="second", db_path=db)
    hist = cs.list_history("k", db_path=db)
    assert [h.version for h in hist] == [2, 1]
    assert hist[0].note == "second"


def test_seed_only_when_absent(db: Path) -> None:
    assert cs.seed_config_if_absent("k", {"seed": True}, db_path=db) is True
    # 2 回目は既に値があるので seed しない
    assert cs.seed_config_if_absent("k", {"seed": "other"}, db_path=db) is False
    assert cs.get_config("k", db_path=db) == {"seed": True}


def test_keys_are_isolated(db: Path) -> None:
    cs.save_config("routing_rules", [1], db_path=db)
    cs.save_config("source_quality", {"cap": 15}, db_path=db)
    assert cs.get_config("routing_rules", db_path=db) == [1]
    assert cs.get_config("source_quality", db_path=db) == {"cap": 15}
