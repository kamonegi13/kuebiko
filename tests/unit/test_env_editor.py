"""src.ui.services.env_editor のテスト (Phase 1.5 + C2 レジストリ動的化)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.tools import channel_registry
from src.tools.channel_registry import BUILTIN_CHANNELS, ChannelDef
from src.ui.services.env_editor import (
    EnvEditError,
    allowed_env_keys,
    env_key_descriptions,
    mask_value,
    parse_env,
    secret_env_keys,
    update_env,
)


@pytest.fixture(autouse=True)
def _builtin_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """テストでは DB を触らず built-in レジストリに固定する。"""
    monkeypatch.setattr(channel_registry, "load_channels", lambda **_: BUILTIN_CHANNELS)


@pytest.fixture()
def _custom_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """built-in + custom channel 1 件のレジストリ。"""
    channels = (
        *BUILTIN_CHANNELS,
        ChannelDef(
            id="vendor_intel",
            label="ベンダ intel",
            webhook_env_key="DISCORD_WEBHOOK_VENDOR_INTEL",
            fallback="watch",
        ),
    )
    monkeypatch.setattr(channel_registry, "load_channels", lambda **_: channels)


class TestMaskValue:
    def test_long_value_retains_prefix(self) -> None:
        assert mask_value("abcdefghij") == "abcd***"

    def test_short_value_fully_masked(self) -> None:
        assert mask_value("abc") == "***"

    def test_empty_value_empty(self) -> None:
        assert mask_value("") == ""


class TestParseEnv:
    def test_parses_kv_pairs(self) -> None:
        out = parse_env("FOO=bar\nBAZ=qux\n")
        assert out == {"FOO": "bar", "BAZ": "qux"}

    def test_ignores_comments_and_blanks(self) -> None:
        text = "# comment\n\nFOO=bar\n# another\nBAZ=qux\n"
        out = parse_env(text)
        assert out == {"FOO": "bar", "BAZ": "qux"}

    def test_strips_value_whitespace(self) -> None:
        out = parse_env("FOO= bar  \n")
        assert out == {"FOO": "bar"}


class TestUpdateEnv:
    def _initial(self, tmp_path: Path) -> Path:
        env = tmp_path / ".env"
        env.write_text(
            "# header\nDISCORD_WEBHOOK_ALERT=https://old\n"
            "# secret\nIMAP_PASSWORD=oldpass\nLOG_LEVEL=INFO\n",
            encoding="utf-8",
        )
        return env

    def test_updates_existing_key(self, tmp_path: Path) -> None:
        env = self._initial(tmp_path)
        update_env(env, {"IMAP_PASSWORD": "newpass"})
        out = parse_env(env.read_text(encoding="utf-8"))
        assert out["IMAP_PASSWORD"] == "newpass"
        assert out["DISCORD_WEBHOOK_ALERT"] == "https://old"
        assert out["LOG_LEVEL"] == "INFO"

    def test_preserves_comments(self, tmp_path: Path) -> None:
        env = self._initial(tmp_path)
        update_env(env, {"LOG_LEVEL": "DEBUG"})
        text = env.read_text(encoding="utf-8")
        assert "# header" in text
        assert "# secret" in text

    def test_adds_new_key_at_end(self, tmp_path: Path) -> None:
        env = self._initial(tmp_path)
        update_env(env, {"TIMEZONE": "Asia/Tokyo"})
        text = env.read_text(encoding="utf-8")
        assert text.rstrip().endswith("TIMEZONE=Asia/Tokyo")

    def test_creates_backup(self, tmp_path: Path) -> None:
        env = self._initial(tmp_path)
        update_env(env, {"LOG_LEVEL": "DEBUG"})
        backup = tmp_path / ".env.bak"
        assert backup.exists()
        assert "DISCORD_WEBHOOK_ALERT=https://old" in backup.read_text(encoding="utf-8")

    def test_rejects_unknown_key(self, tmp_path: Path) -> None:
        env = self._initial(tmp_path)
        with pytest.raises(EnvEditError, match="許可されていない"):
            update_env(env, {"INJECTED_KEY": "evil"})

    def test_atomic_write_does_not_leave_tempfiles(self, tmp_path: Path) -> None:
        env = self._initial(tmp_path)
        update_env(env, {"LOG_LEVEL": "WARN"})
        # 一時ファイルが残っていない
        leftover = list(tmp_path.glob(".env.*.tmp"))
        assert leftover == []


class TestKeySets:
    def test_secret_keys_subset_of_allowed(self) -> None:
        assert set(secret_env_keys()) <= set(allowed_env_keys())

    def test_builtin_webhook_keys_allowed(self) -> None:
        allowed = allowed_env_keys()
        for key in (
            "DISCORD_WEBHOOK_ALERT",
            "DISCORD_WEBHOOK_BRIEF",
            "DISCORD_WEBHOOK_WATCH",
            "DISCORD_WEBHOOK_OPS",
            "DISCORD_WEBHOOK_JAPAN_WATCH",
        ):
            assert key in allowed

    def test_all_channel_webhook_keys_are_secret(self) -> None:
        secrets = set(secret_env_keys())
        assert "DISCORD_WEBHOOK_ALERT" in secrets
        assert "IMAP_PASSWORD" in secrets

    def test_builtin_descriptions_are_curated(self) -> None:
        desc = env_key_descriptions()
        assert desc["DISCORD_WEBHOOK_ALERT"].startswith("[alert]")
        assert "IMAP_HOST" in desc


@pytest.mark.usefixtures("_custom_registry")
class TestCustomChannelKeys:
    """C2: チャンネル追加 → webhook env キーが allowlist / 説明 / secret に連動する。"""

    def test_custom_key_is_allowed_and_secret(self) -> None:
        assert "DISCORD_WEBHOOK_VENDOR_INTEL" in allowed_env_keys()
        assert "DISCORD_WEBHOOK_VENDOR_INTEL" in secret_env_keys()

    def test_custom_key_description_is_generated(self) -> None:
        desc = env_key_descriptions()["DISCORD_WEBHOOK_VENDOR_INTEL"]
        assert "[vendor_intel]" in desc
        assert "ベンダ intel" in desc
        assert "観察チャンネルに自動で振り替えます" in desc

    def test_update_env_accepts_custom_key(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("LOG_LEVEL=INFO\n", encoding="utf-8")
        update_env(env, {"DISCORD_WEBHOOK_VENDOR_INTEL": "https://example.invalid/wh"})
        out = parse_env(env.read_text(encoding="utf-8"))
        assert out["DISCORD_WEBHOOK_VENDOR_INTEL"] == "https://example.invalid/wh"


class TestAnthropicKeyInAllowlist:
    """外部 LLM 開放 (2026-07-19): API キーは .env 編集 allowlist + secret マスク対象。"""

    def test_anthropic_key_allowed_and_secret(self) -> None:
        assert "ANTHROPIC_API_KEY" in allowed_env_keys()
        assert "ANTHROPIC_API_KEY" in secret_env_keys()


def test_displayed_env_keys_hides_unset_anthropic_key() -> None:
    """接続先の対称性 (2026-07-24): ANTHROPIC_API_KEY は未設定時に枠ごと非表示。

    ユーザ定義接続先のキー枠が登録/削除で増減するのと同じライフサイクルにする。
    書込許可 (allowed_env_keys) には常に残る (モデルタブの初回登録経路)。
    """
    from src.ui.services.env_editor import allowed_env_keys, displayed_env_keys

    assert "ANTHROPIC_API_KEY" not in displayed_env_keys({})
    assert "ANTHROPIC_API_KEY" not in displayed_env_keys({"ANTHROPIC_API_KEY": ""})
    assert "ANTHROPIC_API_KEY" in displayed_env_keys({"ANTHROPIC_API_KEY": "sk-ant-x"})
    assert "ANTHROPIC_API_KEY" in allowed_env_keys()
