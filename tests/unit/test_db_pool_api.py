"""DB 接続プール状態 API の契約。

2026-08-25 の全停止の切り分けに 1 時間かかった理由が、プールの内部状態を外から
見る手段が無かったこと。次回は ``GET /api/v1/db-pool`` を最初に見る。
"""

from __future__ import annotations

import pytest

from src.ui.api.db_pool import db_pool_api, get_db_pool_status
from src.ui.read_only_policy import is_public_get


class TestExposure:
    def test_is_not_public(self) -> None:
        """運用情報なので公開面 (Tier0) には出さない。"""
        assert not is_public_get("/api/v1/db-pool")

    def test_is_read_only(self) -> None:
        for route in db_pool_api.routes:
            methods: set[str] = getattr(route, "methods", set())
            assert methods <= {"GET", "HEAD"}

    def test_handler_is_sync(self) -> None:
        """同期 DB 呼び出しを await 無しの async def に置かない (event loop を塞ぐ)。"""
        import inspect

        assert not inspect.iscoroutinefunction(get_db_pool_status)


class TestPayload:
    def test_sqlite_mode_reports_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """dev/tests は SQLite。プールが無いことを明示する (0 と紛らわしくしない)。"""
        monkeypatch.setattr("src.storage.db_backend.is_pg_enabled", lambda: False)

        out = get_db_pool_status()

        assert out["enabled"] is False
        assert "raw" not in out

    def test_reports_leased_available_and_waiting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """枯渇の判断に要る 3 値を出す。leased は size - available で導出する。"""
        monkeypatch.setattr("src.storage.db_backend.is_pg_enabled", lambda: True)

        class _Pool:
            def get_stats(self) -> dict[str, int]:
                return {"pool_size": 20, "pool_available": 0, "pool_max": 20, "requests_waiting": 7}

        monkeypatch.setattr("src.storage.db_backend._get_pool", lambda: _Pool())

        out = get_db_pool_status()

        assert out["leased"] == 20
        assert out["available"] == 0
        assert out["waiting"] == 7
        assert out["saturated"] is True

    def test_healthy_pool_is_not_saturated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("src.storage.db_backend.is_pg_enabled", lambda: True)

        class _Pool:
            def get_stats(self) -> dict[str, int]:
                return {"pool_size": 8, "pool_available": 8, "pool_max": 20, "requests_waiting": 0}

        monkeypatch.setattr("src.storage.db_backend._get_pool", lambda: _Pool())

        out = get_db_pool_status()

        assert out["leased"] == 0
        assert out["saturated"] is False
