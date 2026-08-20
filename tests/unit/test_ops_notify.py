"""ops 通知 seam のテスト (2026-07-05 事故の回帰固定 + 2026-08-21 DB 永続化)。

pipeline_runner の timeout/失敗テストが post_ops_message 経由で実 .env の webhook を叩き、
実 Discord ops を「daily-briefing run 失敗 (runner 検知)」で大量スパムした。テスト実行中は
実投稿しないことを構造的に固定する。

2026-08-21: post_ops_message は webhook 送信の成否に関わらず ops_notices に必ず 1 行
残すようになった。ただし suppress ガード (PYTEST_CURRENT_TEST / READ_ONLY) は DB 書込にも
及ぶため、それを検証するテストは PYTEST_CURRENT_TEST を明示的に外して suppress を解除する
(access_audit テストと同型)。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.storage.run_history import RunHistoryRepository
from src.ui.services import ops_notify


class _DummyConfig:
    """load_app_config() の差し替え用。resolve_webhooks も差し替えるため中身は不問。"""

    discord_webhooks: dict[str, str] = {}


@pytest.mark.asyncio
async def test_post_ops_message_suppressed_under_pytest() -> None:
    # pytest 実行中は PYTEST_CURRENT_TEST が立つため常に no-op で False
    sent = await ops_notify.post_ops_message(title="t", body="b")
    assert sent is False


@pytest.mark.asyncio
async def test_notify_pipeline_failure_does_not_post_under_pytest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 実 config ロード / HTTP を絶対に踏まないこと (踏めば例外化して検出)
    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("テスト中に実 config/HTTP を踏んではならない")

    monkeypatch.setattr("src.config_loader.load_app_config", _boom, raising=False)
    # 例外なく完了する = suppress が config ロード前に効いている
    await ops_notify.notify_pipeline_failure("daily-briefing", 1, "timeout exceeded (0s)")


@pytest.mark.asyncio
async def test_notify_pipeline_partial_does_not_post_under_pytest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # partial 通知 (監査 backlog 2026-07-05) も同じ suppress ガードを通ること
    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("テスト中に実 config/HTTP を踏んではならない")

    monkeypatch.setattr("src.config_loader.load_app_config", _boom, raising=False)
    await ops_notify.notify_pipeline_partial("weekly-recap", 1, ["x: boom"])


def test_explicit_disable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("CTI_DISABLE_OPS_NOTIFY", "1")
    assert ops_notify._is_notify_suppressed() is True
    monkeypatch.setenv("CTI_DISABLE_OPS_NOTIFY", "0")
    # PYTEST_CURRENT_TEST を消した状態で flag=0 → 抑止されない
    assert ops_notify._is_notify_suppressed() is False


# ---------- ops_notices への DB 永続化 (2026-08-21) ----------


class TestPersistOpsNotice:
    """_persist_ops_notice (書き込み seam) 単体のテスト。suppress ガードの外側。"""

    def test_writes_row_with_given_fields(self, tmp_path: Path) -> None:
        repo = RunHistoryRepository(db_path=tmp_path / "persist.db")
        ops_notify._persist_ops_notice(
            title="t", body="b", importance="medium", sent=True, repo=repo
        )
        rows = repo.list_ops_notices()
        assert len(rows) == 1
        assert rows[0]["title"] == "t"
        assert rows[0]["body"] == "b"
        assert rows[0]["importance"] == "medium"
        assert rows[0]["sent"] is True

    def test_writes_row_even_when_sent_is_false(self, tmp_path: Path) -> None:
        repo = RunHistoryRepository(db_path=tmp_path / "persist2.db")
        ops_notify._persist_ops_notice(
            title="unsent", body="b", importance="low", sent=False, repo=repo
        )
        rows = repo.list_ops_notices()
        assert rows[0]["sent"] is False

    def test_swallows_repo_write_failures(self) -> None:
        class _BoomRepo:
            def record_ops_notice(self, **_kwargs: object) -> None:
                raise RuntimeError("db down")

        # 例外を投げずに完了すること (通知経路を書込失敗で壊さない)
        ops_notify._persist_ops_notice(
            title="t",
            body="b",
            importance="low",
            sent=True,
            repo=_BoomRepo(),  # type: ignore[arg-type]
        )


class TestPostOpsMessagePersistence:
    """post_ops_message 経由の永続化 (suppress を明示解除して検証、access_audit と同型)。"""

    def _unsuppress(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.delenv("CTI_DISABLE_OPS_NOTIFY", raising=False)
        monkeypatch.delenv("READ_ONLY", raising=False)
        monkeypatch.setattr("src.config_loader.load_app_config", lambda: _DummyConfig())

    @pytest.mark.asyncio
    async def test_persists_row_on_successful_send(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._unsuppress(monkeypatch)
        monkeypatch.setattr(
            "src.tools.channel_registry.resolve_webhooks",
            lambda _webhooks: {"ops": "https://discord.com/api/webhooks/1/x"},
        )

        async def _ok_post(self: object, message: object, *, attachments: object = None) -> None:
            return None

        monkeypatch.setattr("src.tools.discord_publisher.DiscordPublisher.post", _ok_post)

        repo = RunHistoryRepository(db_path=tmp_path / "ok.db")
        sent = await ops_notify.post_ops_message(title="t", body="b", importance="high", repo=repo)
        assert sent is True
        rows = repo.list_ops_notices()
        assert len(rows) == 1
        assert rows[0]["sent"] is True

    @pytest.mark.asyncio
    async def test_persists_row_even_when_webhook_send_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._unsuppress(monkeypatch)
        monkeypatch.setattr(
            "src.tools.channel_registry.resolve_webhooks",
            lambda _webhooks: {"ops": "https://discord.com/api/webhooks/1/x"},
        )

        async def _boom_post(self: object, message: object, *, attachments: object = None) -> None:
            raise RuntimeError("discord unreachable")

        monkeypatch.setattr("src.tools.discord_publisher.DiscordPublisher.post", _boom_post)

        repo = RunHistoryRepository(db_path=tmp_path / "raise.db")
        sent = await ops_notify.post_ops_message(title="t", body="b", importance="high", repo=repo)
        assert sent is False
        rows = repo.list_ops_notices()
        assert len(rows) == 1
        assert rows[0]["sent"] is False

    @pytest.mark.asyncio
    async def test_persists_row_when_webhook_not_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._unsuppress(monkeypatch)
        monkeypatch.setattr("src.tools.channel_registry.resolve_webhooks", lambda _webhooks: {})

        repo = RunHistoryRepository(db_path=tmp_path / "nowebhook.db")
        sent = await ops_notify.post_ops_message(title="no webhook", body="b", repo=repo)
        assert sent is False
        rows = repo.list_ops_notices()
        assert len(rows) == 1
        assert rows[0]["sent"] is False

    @pytest.mark.asyncio
    async def test_read_only_does_not_write_to_db(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("READ_ONLY", "1")
        repo = RunHistoryRepository(db_path=tmp_path / "readonly.db")
        sent = await ops_notify.post_ops_message(title="t", body="b", repo=repo)
        assert sent is False
        assert repo.list_ops_notices() == []

    @pytest.mark.asyncio
    async def test_pytest_suppression_does_not_write_to_db(self, tmp_path: Path) -> None:
        # PYTEST_CURRENT_TEST はこのテスト実行中も立ったまま (未解除) なので抑止される
        repo = RunHistoryRepository(db_path=tmp_path / "suppressed.db")
        sent = await ops_notify.post_ops_message(title="t", body="b", repo=repo)
        assert sent is False
        assert repo.list_ops_notices() == []


class TestOpsNoticeRetention:
    def test_purge_keeps_recent_and_drops_old(self, tmp_path: Path) -> None:
        repo = RunHistoryRepository(db_path=tmp_path / "retention.db")
        now = datetime.now(UTC)
        repo.record_ops_notice(title="recent", body="b", importance="low", sent=True, when=now)
        repo.record_ops_notice(
            title="old", body="b", importance="low", sent=True, when=now - timedelta(days=200)
        )
        assert len(repo.list_ops_notices()) == 2

        assert repo.purge_old_ops_notices(days=180) == 1
        remaining = repo.list_ops_notices()
        assert [n["title"] for n in remaining] == ["recent"]


class TestOpsNoticeSecretMasking:
    """保存時マスク: read API 漏洩として 2 度再発した型 (config_endpoint_secret_leak) の関門。"""

    @pytest.mark.asyncio
    async def test_persisted_body_is_secret_masked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """例外文字列経由で body に webhook URL / メールが埋まっても保存時にマスクされる。

        DB は pg_dump バックアップにも残留するため、塞ぎ位置は API 応答でなく書込側。
        """
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.delenv("CTI_DISABLE_OPS_NOTIFY", raising=False)
        monkeypatch.delenv("READ_ONLY", raising=False)
        monkeypatch.setattr("src.config_loader.load_app_config", lambda: _DummyConfig())
        monkeypatch.setattr(
            "src.tools.channel_registry.resolve_webhooks",
            lambda _webhooks: {"ops": "https://discord.com/api/webhooks/1/x"},
        )

        async def _ok_post(self: object, message: object, *, attachments: object = None) -> None:
            return None

        monkeypatch.setattr("src.tools.discord_publisher.DiscordPublisher.post", _ok_post)

        repo = RunHistoryRepository(db_path=tmp_path / "mask.db")
        leaky = (
            "post failed: https://discord.com/api/webhooks/12345/secret-token"
            " (contact admin@kuebiko.example)"
        )
        await ops_notify.post_ops_message(title=leaky, body=leaky, importance="high", repo=repo)

        rows = repo.list_ops_notices()
        assert len(rows) == 1
        for field in ("title", "body"):
            assert "secret-token" not in rows[0][field]
            assert "admin@kuebiko.example" not in rows[0][field]
            assert "<masked-webhook-url>" in rows[0][field]
            assert "<masked-email>" in rows[0][field]
