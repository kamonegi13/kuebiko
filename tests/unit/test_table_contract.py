"""tests/unit/_table_contract.py の自己テスト (P0-10)。

既存の表 (ops_notices) で契約が通ることと、契約の各点が違反を見つけることを確かめる。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.storage.row_mappers import _to_iso
from src.storage.run_history import RunHistoryRepository
from tests.unit._table_contract import (
    assert_purge_registered,
    assert_roundtrip,
    assert_table_contract,
)

_WHEN = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def _ops_notice_roundtrip(tmp_path: Path) -> tuple[dict[str, object], dict[str, object]]:
    repo = RunHistoryRepository(db_path=tmp_path / "contract.db")
    repo.record_ops_notice(title="t", body="b", importance="high", sent=True, when=_WHEN)
    row = repo.list_ops_notices(limit=1)[0]
    written: dict[str, object] = {
        "created_at": _to_iso(_WHEN),
        "title": "t",
        "body": "b",
        "importance": "high",
        "sent": True,
    }
    return written, dict(row)


def test_contract_passes_for_existing_table(tmp_path: Path) -> None:
    written, read_back = _ops_notice_roundtrip(tmp_path)
    assert_table_contract(
        "ops_notices",
        tmp_path=tmp_path,
        written=written,
        read_back=read_back,
        purge_method="purge_old_ops_notices",
        api_path="/api/v1/ops-notices",
    )


def test_contract_fails_for_missing_table(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="no_such_table"):
        assert_table_contract(
            "no_such_table",
            tmp_path=tmp_path,
            written={},
            read_back={},
            purge_method="purge_old_ops_notices",
        )


def test_roundtrip_detects_unwritten_column() -> None:
    with pytest.raises(AssertionError, match="書いていない列"):
        assert_roundtrip({"id", "a", "b"}, {"a": 1}, {"a": 1, "b": 2})


def test_roundtrip_detects_unread_column() -> None:
    with pytest.raises(AssertionError, match="読み戻しに無い列"):
        assert_roundtrip({"id", "a", "b"}, {"a": 1, "b": 2}, {"a": 1})


def test_roundtrip_detects_value_mismatch() -> None:
    with pytest.raises(AssertionError, match="違う列"):
        assert_roundtrip({"id", "a"}, {"a": 1}, {"a": 2})


def test_roundtrip_excludes_auto_columns() -> None:
    assert_roundtrip({"id", "a"}, {"a": 1}, {"a": 1})


def test_purge_must_exist_on_repo() -> None:
    with pytest.raises(AssertionError, match="repo に"):
        assert_purge_registered("purge_that_does_not_exist")


def test_purge_must_be_called_from_maintenance() -> None:
    # repo に存在するが保守から呼ばれていない method は未登録として落ちる
    with pytest.raises(AssertionError, match="maintenance"):
        assert_purge_registered("list_ops_notices")


def test_public_api_path_is_detected(tmp_path: Path) -> None:
    written, read_back = _ops_notice_roundtrip(tmp_path)
    with pytest.raises(AssertionError, match="匿名で読める"):
        assert_table_contract(
            "ops_notices",
            tmp_path=tmp_path,
            written=written,
            read_back=read_back,
            purge_method="purge_old_ops_notices",
            api_path="/api/health",
        )
