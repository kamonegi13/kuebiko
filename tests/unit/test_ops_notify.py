"""ops 通知 seam のテスト (2026-07-05 事故の回帰固定)。

pipeline_runner の timeout/失敗テストが post_ops_message 経由で実 .env の webhook を叩き、
実 Discord ops を「daily-briefing run 失敗 (runner 検知)」で大量スパムした。テスト実行中は
実投稿しないことを構造的に固定する。
"""

from __future__ import annotations

import pytest

from src.ui.services import ops_notify


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
