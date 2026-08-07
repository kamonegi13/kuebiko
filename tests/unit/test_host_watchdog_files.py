"""ホスト常駐 watchdog との受け渡し契約テスト (2026-08-02)。

不変量:
- 有効/無効は**フラグファイルの有無**で表す (mobile tunnel と同じ契約)。
- 未導入 / 未実行なら状態は空 dict (「導入済みだが停止」と「未導入」を区別できる)。
- 読み取りはファイル不在・壊れた JSON でも例外を投げない (UI を落とさない)。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.tools import host_watchdog_files as hwf


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(hwf, "ENABLED_FLAG_FILE", tmp_path / ".orbstack_watchdog_enabled")
    monkeypatch.setattr(hwf, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(hwf, "LOG_FILE", tmp_path / "watchdog.log")
    return tmp_path


class TestEnableFlag:
    def test_absent_flag_means_disabled(self, paths: Path) -> None:
        assert hwf.is_enabled() is False

    def test_toggle_round_trip(self, paths: Path) -> None:
        hwf.set_enabled(True)
        assert hwf.is_enabled() is True
        hwf.set_enabled(False)
        assert hwf.is_enabled() is False

    def test_disable_is_idempotent(self, paths: Path) -> None:
        # 既に無効でも例外を投げない (UI の二重クリック耐性)
        hwf.set_enabled(False)
        hwf.set_enabled(False)
        assert hwf.is_enabled() is False

    def test_enable_creates_missing_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        nested = tmp_path / "not-yet" / ".flag"
        monkeypatch.setattr(hwf, "ENABLED_FLAG_FILE", nested)
        hwf.set_enabled(True)
        assert nested.exists()


class TestState:
    def test_missing_state_is_empty(self, paths: Path) -> None:
        # 未導入と「導入済みだが異常」を区別するため、空 dict で返す
        assert hwf.read_state() == {}

    def test_corrupt_state_does_not_raise(self, paths: Path) -> None:
        (paths / "state.json").write_text("{ broken", encoding="utf-8")
        assert hwf.read_state() == {}

    def test_reads_written_state(self, paths: Path) -> None:
        payload = {"status": "healthy", "consecutive_failures": 0}
        (paths / "state.json").write_text(json.dumps(payload), encoding="utf-8")
        assert hwf.read_state()["status"] == "healthy"


class TestLogTail:
    def test_missing_log_is_empty(self, paths: Path) -> None:
        assert hwf.read_log_tail() == []

    def test_returns_newest_first_and_respects_limit(self, paths: Path) -> None:
        (paths / "watchdog.log").write_text("\n".join(f"line{i}" for i in range(10)))
        tail = hwf.read_log_tail(limit=3)
        assert tail == ["line9", "line8", "line7"]


class TestHealthCheckSurfacing:
    """死活監視 widget への露出は**異常時のみ** (2026-08-02)。

    正常時に 1 行増やすと、本当に見るべき異常が埋もれる。逆に「自動復旧した」は
    出す — 黙って直すとスリープ復帰失敗の傾向が見えなくなるため。
    """

    def _check(self, state: dict[str, object], paths: Path) -> object:
        from src.ui.services.health import _host_watchdog_check

        (paths / "state.json").write_text(json.dumps(state), encoding="utf-8")
        hwf.set_enabled(True)
        return _host_watchdog_check()

    def test_healthy_is_not_surfaced(self, paths: Path) -> None:
        assert self._check({"status": "healthy"}, paths) is None

    def test_not_installed_is_not_surfaced(self, paths: Path) -> None:
        from src.ui.services.health import _host_watchdog_check

        hwf.set_enabled(True)
        assert _host_watchdog_check() is None  # state.json が無い = 未導入

    def test_disabled_is_not_surfaced(self, paths: Path) -> None:
        from src.ui.services.health import _host_watchdog_check

        (paths / "state.json").write_text(json.dumps({"status": "degraded"}), encoding="utf-8")
        hwf.set_enabled(False)
        assert _host_watchdog_check() is None  # 無効化中は「異常」ではない

    def test_degraded_is_warning(self, paths: Path) -> None:
        check = self._check({"status": "degraded", "consecutive_failures": 2}, paths)
        assert check is not None
        assert check.status == "warning"  # type: ignore[attr-defined]
        assert "2 回連続" in check.detail  # type: ignore[attr-defined]

    def test_recovery_failed_is_error(self, paths: Path) -> None:
        check = self._check({"status": "recovery_failed", "detail": "上限に到達"}, paths)
        assert check is not None
        assert check.status == "error"  # type: ignore[attr-defined]

    def test_recovered_is_surfaced_as_warning(self, paths: Path) -> None:
        # 自動で直っても「起きた事実」は運用シグナルなので出す
        check = self._check({"status": "recovered", "last_recovery_at": "2026-08-02T17:00"}, paths)
        assert check is not None
        assert check.status == "warning"  # type: ignore[attr-defined]
