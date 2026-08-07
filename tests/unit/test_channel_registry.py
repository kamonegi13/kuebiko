"""channel registry (C1: チャンネル config-driven 化 backend) のテスト。

中核検証 = 「6 built-in の channel→webhook 解決が legacy 実装と完全同一」。
DB (config_store key "channels") が空なら built-in にフォールバックし、
DB に保存されたレジストリがあればそれを正とする。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path

import pytest

from src.storage.config_store import save_config
from src.tools.channel_registry import (
    BUILTIN_CHANNELS,
    CHANNELS_CONFIG_KEY,
    ChannelDef,
    fallback_map,
    invalidate_channels_cache,
    known_channel_ids,
    load_channels,
    order_map,
    push_map,
    resolve_webhooks,
    routable_channel_ids,
    seed_channels_if_absent,
    validate_channels,
)

# built-in 5 ch の基準値 (grok_daily は 2026-06-13 廃止)
LEGACY_CHANNEL_IDS = {"alert", "brief", "watch", "ops", "japan_watch"}
LEGACY_FALLBACK = {"japan_watch": "alert"}
LEGACY_ORDER = {
    "alert": 0,
    "japan_watch": 1,
    "brief": 3,
    "watch": 4,
    "ops": 5,
}
# routing rules engine が許容する投稿先 (ops は routing 対象外)
LEGACY_ROUTABLE = {"alert", "brief", "watch", "japan_watch"}


@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """SQLite fallback の一時 DB。テストごとにキャッシュを無効化する。"""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    invalidate_channels_cache()
    yield tmp_path / "test_channels.db"
    invalidate_channels_cache()


def _builtin_env(values: dict[str, str] | None = None) -> dict[str, str]:
    """AppConfig.discord_webhooks 相当の builtin env 値 (channel id → URL)。"""
    base = dict.fromkeys(LEGACY_CHANNEL_IDS, "")
    if values:
        base.update(values)
    return base


class TestBuiltinDefinitions:
    def test_builtin_matches_legacy_six_channels(self) -> None:
        assert {c.id for c in BUILTIN_CHANNELS} == LEGACY_CHANNEL_IDS

    def test_builtin_fallback_matches_legacy(self) -> None:
        assert {c.id: c.fallback for c in BUILTIN_CHANNELS if c.fallback} == LEGACY_FALLBACK

    def test_builtin_order_matches_legacy(self) -> None:
        assert {c.id: c.order for c in BUILTIN_CHANNELS} == LEGACY_ORDER

    def test_builtin_routable_excludes_ops_only(self) -> None:
        assert {c.id for c in BUILTIN_CHANNELS if c.routable} == LEGACY_ROUTABLE

    def test_builtin_env_keys_follow_convention(self) -> None:
        for c in BUILTIN_CHANNELS:
            assert c.webhook_env_key == f"DISCORD_WEBHOOK_{c.id.upper()}"


class TestLoadChannels:
    def test_returns_builtin_when_db_empty(self, db_path: Path) -> None:
        assert load_channels(db_path=db_path) == BUILTIN_CHANNELS

    def test_seed_then_load_roundtrip(self, db_path: Path) -> None:
        assert seed_channels_if_absent(db_path=db_path) is True
        # 2 回目は no-op
        assert seed_channels_if_absent(db_path=db_path) is False
        assert load_channels(db_path=db_path) == BUILTIN_CHANNELS

    def test_db_value_takes_precedence_over_builtin(self, db_path: Path) -> None:
        custom = [
            {"id": "alert", "label": "即応", "webhook_env_key": "DISCORD_WEBHOOK_ALERT"},
            {
                "id": "vendor_intel",
                "label": "ベンダ intel",
                "webhook_env_key": "DISCORD_WEBHOOK_VENDOR_INTEL",
                "fallback": "watch",
                "order": 10,
            },
        ]
        save_config(CHANNELS_CONFIG_KEY, custom, db_path=db_path)
        loaded = load_channels(db_path=db_path)
        assert [c.id for c in loaded] == ["alert", "vendor_intel"]
        assert loaded[1] == ChannelDef(
            id="vendor_intel",
            label="ベンダ intel",
            webhook_env_key="DISCORD_WEBHOOK_VENDOR_INTEL",
            fallback="watch",
            order=10,
        )

    def test_invalid_entries_are_skipped(self, db_path: Path) -> None:
        broken = [
            {"label": "id なし"},
            {"id": "", "webhook_env_key": "X"},
            {"id": "ok", "label": "正常", "webhook_env_key": "DISCORD_WEBHOOK_OK"},
        ]
        save_config(CHANNELS_CONFIG_KEY, broken, db_path=db_path)
        loaded = load_channels(db_path=db_path)
        assert [c.id for c in loaded] == ["ok"]

    def test_all_invalid_falls_back_to_builtin(self, db_path: Path) -> None:
        save_config(CHANNELS_CONFIG_KEY, [{"label": "壊"}], db_path=db_path)
        assert load_channels(db_path=db_path) == BUILTIN_CHANNELS


class TestDerivedMaps:
    def test_known_channel_ids_matches_legacy(self, db_path: Path) -> None:
        assert known_channel_ids(db_path=db_path) == frozenset(LEGACY_CHANNEL_IDS)

    def test_routable_channel_ids_matches_legacy(self, db_path: Path) -> None:
        assert routable_channel_ids(db_path=db_path) == frozenset(LEGACY_ROUTABLE)

    def test_fallback_map_matches_legacy(self, db_path: Path) -> None:
        assert fallback_map(db_path=db_path) == LEGACY_FALLBACK

    def test_order_map_matches_legacy(self, db_path: Path) -> None:
        assert order_map(db_path=db_path) == LEGACY_ORDER


class TestResolveWebhooks:
    def test_builtin_resolution_identical_to_legacy_dict(self, db_path: Path) -> None:
        """旧 AppConfig.discord_webhooks の静的 dict と完全同一 (空文字含む)。"""
        env = _builtin_env({"alert": "https://example.invalid/wh/a", "brief": ""})
        assert resolve_webhooks(env, db_path=db_path) == env

    def test_custom_channel_resolves_from_environ(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_config(
            CHANNELS_CONFIG_KEY,
            [
                {"id": "alert", "webhook_env_key": "DISCORD_WEBHOOK_ALERT"},
                {"id": "custom", "webhook_env_key": "DISCORD_WEBHOOK_CUSTOM"},
            ],
            db_path=db_path,
        )
        monkeypatch.setenv("DISCORD_WEBHOOK_CUSTOM", "https://example.invalid/wh/c")
        env = _builtin_env({"alert": "https://example.invalid/wh/a"})
        resolved = resolve_webhooks(env, db_path=db_path)
        assert resolved == {
            "alert": "https://example.invalid/wh/a",
            "custom": "https://example.invalid/wh/c",
        }

    def test_disabled_channel_is_excluded(self, db_path: Path) -> None:
        save_config(
            CHANNELS_CONFIG_KEY,
            [
                {"id": "alert", "webhook_env_key": "DISCORD_WEBHOOK_ALERT"},
                {"id": "watch", "webhook_env_key": "DISCORD_WEBHOOK_WATCH", "enabled": False},
            ],
            db_path=db_path,
        )
        resolved = resolve_webhooks(_builtin_env(), db_path=db_path)
        assert "watch" not in resolved
        assert "alert" in resolved

    def test_renamed_channel_keeps_builtin_env_value(self, db_path: Path) -> None:
        """builtin env key を保ったまま channel id を変えても AppConfig 値で解決する。"""
        save_config(
            CHANNELS_CONFIG_KEY,
            [{"id": "alert_v2", "webhook_env_key": "DISCORD_WEBHOOK_ALERT"}],
            db_path=db_path,
        )
        env = _builtin_env({"alert": "https://example.invalid/wh/a"})
        assert resolve_webhooks(env, db_path=db_path) == {
            "alert_v2": "https://example.invalid/wh/a",
        }


class TestValidateChannels:
    """C3: チャンネル管理 UI の保存前検証 (silent typo / 危険変更の防止)。"""

    def _builtin_payload(self) -> list[dict[str, object]]:
        return [asdict(c) for c in BUILTIN_CHANNELS]

    def test_builtin_passthrough_is_valid(self) -> None:
        assert validate_channels(self._builtin_payload(), rule_channels=set()) == []

    def test_custom_channel_is_valid(self) -> None:
        payload: list[dict[str, object]] = [
            *self._builtin_payload(),
            {
                "id": "vendor_intel",
                "label": "ベンダ intel",
                "webhook_env_key": "DISCORD_WEBHOOK_VENDOR_INTEL",
                "fallback": "watch",
            },
        ]
        assert validate_channels(payload, rule_channels=set()) == []

    def test_missing_builtin_is_rejected(self) -> None:
        payload = [d for d in self._builtin_payload() if d["id"] != "ops"]
        errs = validate_channels(payload, rule_channels=set())
        assert any("ops" in e for e in errs)

    def test_bad_id_format_rejected(self) -> None:
        payload: list[dict[str, object]] = [
            *self._builtin_payload(),
            {"id": "Bad-ID", "webhook_env_key": "DISCORD_WEBHOOK_X"},
        ]
        errs = validate_channels(payload, rule_channels=set())
        assert any("Bad-ID" in e for e in errs)

    def test_env_key_must_have_webhook_prefix(self) -> None:
        """任意 env キー (例 DATABASE_URL) を UI 編集可能にする抜け穴を塞ぐ。"""
        payload: list[dict[str, object]] = [
            *self._builtin_payload(),
            {"id": "evil", "webhook_env_key": "DATABASE_URL"},
        ]
        errs = validate_channels(payload, rule_channels=set())
        assert any("DISCORD_WEBHOOK_" in e for e in errs)

    def test_alert_push_cannot_be_disabled(self) -> None:
        """通知再設計: alert は緊急 push の最終 ch なので push=False は不可。"""
        payload = [
            {**asdict(c), "push": False} if c.id == "alert" else asdict(c) for c in BUILTIN_CHANNELS
        ]
        errs = validate_channels(payload, rule_channels=set())
        assert any("alert" in e and "push" in e for e in errs)

    def test_non_alert_push_false_is_valid(self) -> None:
        """watch/brief/japan_watch を push=False (保存のみ) にするのは許可。"""
        payload = [
            {**asdict(c), "push": False} if c.id in ("watch", "brief", "japan_watch") else asdict(c)
            for c in BUILTIN_CHANNELS
        ]
        assert validate_channels(payload, rule_channels=set()) == []

    def test_duplicate_id_and_env_key_rejected(self) -> None:
        payload: list[dict[str, object]] = [
            *self._builtin_payload(),
            {"id": "alert", "webhook_env_key": "DISCORD_WEBHOOK_DUP1"},
            {"id": "dup2", "webhook_env_key": "DISCORD_WEBHOOK_ALERT"},
        ]
        errs = validate_channels(payload, rule_channels=set())
        assert any("重複" in e for e in errs)

    def test_fallback_must_reference_existing_other_channel(self) -> None:
        payload: list[dict[str, object]] = [
            *self._builtin_payload(),
            {"id": "c1", "webhook_env_key": "DISCORD_WEBHOOK_C1", "fallback": "nope"},
            {"id": "c2", "webhook_env_key": "DISCORD_WEBHOOK_C2", "fallback": "c2"},
        ]
        errs = validate_channels(payload, rule_channels=set())
        assert any("c1" in e for e in errs)
        assert any("c2" in e for e in errs)

    def test_builtin_env_key_and_routable_immutable(self) -> None:
        payload = self._builtin_payload()
        payload[0] = {**payload[0], "webhook_env_key": "DISCORD_WEBHOOK_RENAMED"}
        ops = next(d for d in payload if d["id"] == "ops")
        ops["routable"] = True
        errs = validate_channels(payload, rule_channels=set())
        assert any("webhook_env_key" in e for e in errs)
        assert any("routable" in e for e in errs)

    def test_rule_referenced_channel_must_exist_and_be_enabled(self) -> None:
        payload = self._builtin_payload()
        watch = next(d for d in payload if d["id"] == "watch")
        watch["enabled"] = False
        errs = validate_channels(payload, rule_channels={"watch", "ghost"})
        assert any("watch" in e for e in errs)
        assert any("ghost" in e for e in errs)


class TestPushAttribute:
    """通知再設計: channel push 属性 (Discord 配信 / 保存のみ web-only)。"""

    def test_builtin_all_push_by_default(self) -> None:
        # 既定は全 tier 配信 (web-only はユーザーが UI でオプトイン)。
        assert all(c.push for c in BUILTIN_CHANNELS)

    def test_parse_defaults_push_true_when_absent(self, db_path: Path) -> None:
        # 旧 schema (push キー無し) は True にフォールバック (後方互換)。
        payload = [{k: v for k, v in asdict(c).items() if k != "push"} for c in BUILTIN_CHANNELS]
        save_config(CHANNELS_CONFIG_KEY, payload, db_path=db_path)
        invalidate_channels_cache()
        chans = {c.id: c for c in load_channels(db_path=db_path)}
        assert chans["watch"].push is True

    def test_push_false_roundtrips(self, db_path: Path) -> None:
        payload = [
            {**asdict(c), "push": False} if c.id in ("watch", "brief", "japan_watch") else asdict(c)
            for c in BUILTIN_CHANNELS
        ]
        save_config(CHANNELS_CONFIG_KEY, payload, db_path=db_path)
        invalidate_channels_cache()
        pm = push_map(db_path=db_path)
        assert pm == {
            "alert": True,
            "japan_watch": False,
            "brief": False,
            "watch": False,
            "ops": True,
        }


class TestCache:
    def test_cache_returns_stale_until_invalidated(self, db_path: Path) -> None:
        first = load_channels(db_path=db_path)
        assert first == BUILTIN_CHANNELS
        save_config(
            CHANNELS_CONFIG_KEY,
            [{"id": "only", "webhook_env_key": "DISCORD_WEBHOOK_ONLY"}],
            db_path=db_path,
        )
        # キャッシュ有効中は旧値
        assert load_channels(db_path=db_path) == BUILTIN_CHANNELS
        invalidate_channels_cache()
        assert [c.id for c in load_channels(db_path=db_path)] == ["only"]
