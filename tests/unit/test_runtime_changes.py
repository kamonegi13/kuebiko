"""実行時の構成変更の記録 (2026-11-20、11-19 監査 P1-5 の最小構成)。

s26r 切替で出力の分布がずれたのに、いつ何が変わったか (環境変数の旗・コード) が
DB に残っておらず切り分けられなかった。起動のたびに「コードの指紋」と「秘密を含まない
旗の値」を集め、前回と違うときだけ 1 行記録する。モデル割当と基準の版は既存の版履歴
(app_config_versions) にあるので写さない。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.ops.runtime_changes import (
    build_runtime_change_line,
    code_fingerprint,
    record_runtime_change,
    snapshot_flags,
)
from src.storage.run_history import RunHistoryRepository

NOW = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)


class TestSnapshotFlags:
    def test_keeps_short_flag_values(self) -> None:
        env = {"SLOT_RENDER": "1", "GEO_RESCUE_MODE": "analysis", "TTP_LIMIT": "shadow"}
        assert snapshot_flags(env) == env

    def test_drops_secret_like_names(self) -> None:
        env = {
            "ANTHROPIC_API_KEY": "1",
            "DISCORD_WEBHOOK_ALERT": "x",
            "POSTGRES_PASSWORD": "short",
            "CLOUDFLARE_TUNNEL_TOKEN": "abc",
            "DATABASE_URL": "postgresql://u@h/d",
            "CF_ACCESS_AUD": "abc",
            "CLOUDFLARE_TUNNEL_HOSTNAME": "ops.kuebiko.example",
            "IMAP_USER": "someone",
        }
        assert snapshot_flags(env) == {}

    def test_drops_long_or_odd_values(self) -> None:
        env = {"SOME_FLAG": "a" * 80, "OTHER": "has space", "PATHY": "/app/data/x"}
        assert snapshot_flags(env) == {}

    def test_drops_non_flag_names(self) -> None:
        assert snapshot_flags({"path": "1", "HOME": "1", "LANG": "ja"}) == {}


def test_code_fingerprint_changes_with_content(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    first = code_fingerprint(tmp_path)
    (tmp_path / "src" / "a.py").write_text("x = 2\n", encoding="utf-8")
    assert code_fingerprint(tmp_path) != first


def test_records_only_when_changed(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "r.db")
    env = {"SLOT_RENDER": "1"}
    assert record_runtime_change(repo, code_sha="c1", env=env, now=NOW) is True
    assert record_runtime_change(repo, code_sha="c1", env=env, now=NOW) is False
    assert record_runtime_change(repo, code_sha="c1", env={"SLOT_RENDER": "0"}, now=NOW) is True
    assert record_runtime_change(repo, code_sha="c2", env={"SLOT_RENDER": "0"}, now=NOW) is True


def test_heartbeat_line_lists_changed_flags(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "r.db")
    old = NOW - timedelta(days=10)
    record_runtime_change(repo, code_sha="c1", env={"A_FLAG": "0", "B_FLAG": "1"}, now=old)
    record_runtime_change(repo, code_sha="c2", env={"A_FLAG": "1", "B_FLAG": "1"}, now=NOW)
    line = build_runtime_change_line(repo, now=NOW + timedelta(hours=1))
    assert line is not None
    assert "1 回" in line and "A_FLAG" in line and "B_FLAG" not in line
    assert "コード" in line


def test_heartbeat_line_none_changes(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "r.db")
    line = build_runtime_change_line(repo, now=NOW)
    assert line == "構成変更 (24h): なし"


def test_heartbeat_extra_line_with_warning_raises_importance() -> None:
    from src.ui.services.source_health import build_heartbeat_text

    _, body, importance = build_heartbeat_text(
        run_counts={"succeeded": 1},
        silent=[],
        feeds_total=1,
        extra_lines=("構成変更 (24h): なし", None, "⚠️ 何か"),
    )
    assert "構成変更 (24h): なし" in body and "⚠️ 何か" in body
    assert importance == "medium"
