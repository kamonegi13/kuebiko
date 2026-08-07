"""Discord チャンネルレジストリ (C1: チャンネル config-driven 化 backend)。

従来コードに散在していたチャンネル定義 (Literal / _CHANNEL_FALLBACK /
_CHANNEL_ORDER / VALID_CHANNELS / webhook 6 固定フィールド) を 1 つの
レジストリに集約する。**DB (config_store key "channels") を正**とし、
未投入・障害時は built-in 6 チャンネルに fail-safe する
(routing_rules / source_quality と同じパターン)。

webhook URL は秘密のためレジストリには置かず、各チャンネルは
``webhook_env_key`` (例 ``DISCORD_WEBHOOK_ALERT``) で .env / 環境変数を参照する。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.logging_config import get_logger

_log = get_logger(__name__)

CHANNELS_CONFIG_KEY = "channels"

# resolve_webhooks の .env fallback (custom channel 用)。CWD 直下 = AppConfig と同じ参照先。
_DEFAULT_DOTENV_PATH = Path(".env")

# custom channel の order 既定値 (built-in 6 の後ろに並ぶ)
DEFAULT_CHANNEL_ORDER = 99


@dataclass(frozen=True)
class ChannelDef:
    """Discord 投稿先チャンネル 1 件の定義。

    Attributes:
        id: チャンネル識別子 (routing / metadata / publishers の key)。
        label: UI 表示名。
        webhook_env_key: webhook URL を保持する .env / 環境変数のキー。
        enabled: 無効化すると webhook 解決から除外される (投稿先として消える)。
        routable: routing rules engine の投稿先として許容するか (ops は False)。
        push: Discord に配信するか。False = 「保存のみ (web-only)」で routing は当該 channel に
            振り分けるが投稿ループは Discord push をスキップし DB に status='posted' で保存する
            (全 web/分析サーフェスには出る)。通知再設計 (Discord=警告 / Web=状況認識) の制御点。
            既定 True (= 従来どおり配信)。朝刊/夕刊ダイジェストは投稿ループ外の別レールのため
            brief.push=False でも配信され続ける (#brief = ダイジェスト専用になる)。
        fallback: webhook 未設定時に投稿を流す先の channel id (無指定は投稿失敗扱い)。
        order: 投稿順 / ch 横断 dedup の優先順 (小さいほど上位)。

    表示は色 + 名前 (UI 側 channelColor で built-in 固定色 / custom 自動色)。
    per-channel アイコンは「色+名前で十分・custom で無意味に退化」のため不採用 (2026-06-13)。
    """

    id: str
    label: str = ""
    webhook_env_key: str = ""
    enabled: bool = True
    routable: bool = True
    push: bool = True
    fallback: str | None = None
    order: int = DEFAULT_CHANNEL_ORDER


# built-in 5 チャンネル (doctrine ベース)。order は旧 main.py _CHANNEL_ORDER、
# fallback は旧 _CHANNEL_FALLBACK、routable は旧 routing_rules.VALID_CHANNELS と完全同一。
BUILTIN_CHANNELS: tuple[ChannelDef, ...] = (
    ChannelDef(id="alert", label="即応", webhook_env_key="DISCORD_WEBHOOK_ALERT", order=0),
    ChannelDef(
        id="japan_watch",
        label="日本関連",
        webhook_env_key="DISCORD_WEBHOOK_JAPAN_WATCH",
        fallback="alert",
        order=1,
    ),
    ChannelDef(id="brief", label="朝刊", webhook_env_key="DISCORD_WEBHOOK_BRIEF", order=3),
    ChannelDef(id="watch", label="観察", webhook_env_key="DISCORD_WEBHOOK_WATCH", order=4),
    ChannelDef(
        id="ops",
        label="運用",
        webhook_env_key="DISCORD_WEBHOOK_OPS",
        routable=False,
        order=5,
    ),
)

_BUILTIN_ID_BY_ENV_KEY: dict[str, str] = {c.webhook_env_key: c.id for c in BUILTIN_CHANNELS}

# load_channels のプロセス内キャッシュ (routing_rules._RULES_CACHE と同パターン)。
# key = str(db_path) (None 含む)。UI 保存時は invalidate_channels_cache() を呼ぶ。
_CHANNELS_CACHE: dict[str, tuple[ChannelDef, ...]] = {}


def _parse_entry(raw: Any) -> ChannelDef | None:
    """DB の 1 entry を ChannelDef に変換。必須欠落は None (呼び出し側で skip)。"""
    if not isinstance(raw, dict):
        return None
    channel_id = raw.get("id")
    env_key = raw.get("webhook_env_key")
    if not isinstance(channel_id, str) or not channel_id:
        return None
    if not isinstance(env_key, str) or not env_key:
        return None
    fallback = raw.get("fallback")
    order = raw.get("order")
    # 旧 schema の icon キーは無視 (per-channel icon は 2026-06-13 廃止)
    return ChannelDef(
        id=channel_id,
        label=str(raw.get("label") or channel_id),
        webhook_env_key=env_key,
        enabled=bool(raw.get("enabled", True)),
        routable=bool(raw.get("routable", True)),
        # push 未指定 (旧 schema) は True = 従来どおり配信 (後方互換)。
        push=bool(raw.get("push", True)),
        fallback=fallback if isinstance(fallback, str) and fallback else None,
        order=order if isinstance(order, int) else DEFAULT_CHANNEL_ORDER,
    )


def load_channels(*, db_path: Path | None = None) -> tuple[ChannelDef, ...]:
    """チャンネルレジストリを返す。**DB (config_store) を正**とする。

    DB 未投入 / 全 entry 不正 / DB 障害時は built-in 6 チャンネルに degrade
    (投稿可否の最重要パスを registry 障害で殺さない fail-safe)。
    """
    cache_key = str(db_path)
    cached = _CHANNELS_CACHE.get(cache_key)
    if cached is not None:
        return cached

    channels = _load_channels_uncached(db_path=db_path)
    _CHANNELS_CACHE[cache_key] = channels
    return channels


def _load_channels_uncached(*, db_path: Path | None) -> tuple[ChannelDef, ...]:
    try:
        from src.storage.config_store import get_config

        raw = get_config(CHANNELS_CONFIG_KEY, db_path=db_path)
    except Exception as e:  # noqa: BLE001 — DB 障害で投稿経路を壊さない
        _log.warning("channel_registry_db_load_failed", error=str(e))
        return BUILTIN_CHANNELS
    if raw is None:
        return BUILTIN_CHANNELS
    if not isinstance(raw, list):
        _log.warning("channel_registry_invalid_shape", type=type(raw).__name__)
        return BUILTIN_CHANNELS

    parsed: list[ChannelDef] = []
    for entry in raw:
        ch = _parse_entry(entry)
        if ch is None:
            _log.warning("channel_registry_entry_skipped", entry=str(entry)[:200])
            continue
        parsed.append(ch)
    if not parsed:
        _log.warning("channel_registry_empty_after_parse_fallback_builtin")
        return BUILTIN_CHANNELS
    return tuple(parsed)


def invalidate_channels_cache() -> None:
    """UI 保存後などにキャッシュを破棄する (次回 load で DB を読み直す)。"""
    _CHANNELS_CACHE.clear()


def seed_channels_if_absent(*, db_path: Path | None = None) -> bool:
    """DB 未投入なら built-in 6 チャンネルを version 1 として取り込む (起動時)。"""
    from dataclasses import asdict

    from src.storage.config_store import seed_config_if_absent

    return seed_config_if_absent(
        CHANNELS_CONFIG_KEY,
        [asdict(c) for c in BUILTIN_CHANNELS],
        note="初期 seed (built-in 6 チャンネル)",
        db_path=db_path,
    )


def known_channel_ids(*, db_path: Path | None = None) -> frozenset[str]:
    """既知チャンネル id の集合 (target_channel 型ガード等に使う)。"""
    return frozenset(c.id for c in load_channels(db_path=db_path))


def routable_channel_ids(*, db_path: Path | None = None) -> frozenset[str]:
    """routing rules の投稿先として許容するチャンネル id (旧 VALID_CHANNELS)。"""
    return frozenset(c.id for c in load_channels(db_path=db_path) if c.routable)


def fallback_map(*, db_path: Path | None = None) -> dict[str, str]:
    """webhook 未設定時の fallback 先 (旧 main._CHANNEL_FALLBACK)。"""
    return {c.id: c.fallback for c in load_channels(db_path=db_path) if c.fallback}


def order_map(*, db_path: Path | None = None) -> dict[str, int]:
    """投稿順 / dedup 優先順 (旧 main._CHANNEL_ORDER)。"""
    return {c.id: c.order for c in load_channels(db_path=db_path)}


def push_map(*, db_path: Path | None = None) -> dict[str, bool]:
    """channel id → Discord 配信するか (push)。False = 保存のみ (web-only)。

    通知再設計の制御点。投稿ループはこの map を見て push=False の tier を save-only にする。
    """
    return {c.id: c.push for c in load_channels(db_path=db_path)}


# ---------- 保存前検証 (C3: チャンネル管理 UI) ----------

_CHANNEL_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# UI から allowlist 化される env キーは webhook 用 prefix に限定する
# (任意キー名を許すと DATABASE_URL 等の機密 env が .env editor で編集可能になる抜け穴)。
WEBHOOK_ENV_KEY_PREFIX = "DISCORD_WEBHOOK_"
_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_MAX_CHANNEL_ORDER = 999


def validate_channels(  # noqa: PLR0912 — 検証項目の列挙で分岐が多いのは仕様
    raw: list[dict[str, Any]],
    *,
    rule_channels: set[str],
) -> list[str]:
    """チャンネルレジストリの保存前検証。エラー文字列の list を返す (空 = OK)。

    Args:
        raw: 保存しようとする channel entry の list (dict 形式)。
        rule_channels: 現行 routing rules が参照している channel id 集合
            (参照中の channel の削除 / 無効化を防ぐガード)。
    """
    if not isinstance(raw, list) or not raw:
        return ["channels は空でない list である必要があります"]

    errs: list[str] = []
    seen_ids: set[str] = set()
    seen_env_keys: set[str] = set()
    parsed: dict[str, ChannelDef] = {}
    for i, entry in enumerate(raw):
        ch = _parse_entry(entry)
        if ch is None:
            errs.append(f"channels[{i}]: id / webhook_env_key が必要です")
            continue
        if not _CHANNEL_ID_RE.match(ch.id):
            errs.append(f"channels[{i}]: id '{ch.id}' は ^[a-z][a-z0-9_]{{0,31}}$ に一致が必要")
        if not ch.webhook_env_key.startswith(WEBHOOK_ENV_KEY_PREFIX) or not _ENV_KEY_RE.match(
            ch.webhook_env_key
        ):
            errs.append(
                f"channels[{i}]: webhook_env_key '{ch.webhook_env_key}' は "
                f"{WEBHOOK_ENV_KEY_PREFIX} で始まる大文字キーが必要"
            )
        if ch.id in seen_ids:
            errs.append(f"channels[{i}]: id '{ch.id}' が重複しています")
        if ch.webhook_env_key in seen_env_keys:
            errs.append(f"channels[{i}]: webhook_env_key '{ch.webhook_env_key}' が重複しています")
        if not 0 <= ch.order <= _MAX_CHANNEL_ORDER:
            errs.append(f"channels[{i}]: order は 0-{_MAX_CHANNEL_ORDER} の範囲が必要")
        seen_ids.add(ch.id)
        seen_env_keys.add(ch.webhook_env_key)
        parsed[ch.id] = ch

    # fallback は自身以外の既存 channel を指すこと
    for ch in parsed.values():
        if ch.fallback is None:
            continue
        if ch.fallback == ch.id:
            errs.append(f"channel '{ch.id}': fallback に自分自身は指定できません")
        elif ch.fallback not in parsed:
            errs.append(f"channel '{ch.id}': fallback '{ch.fallback}' が存在しません")

    # built-in 6 はコードパス (ops 通知 / digest=brief 等) が前提とするため削除不可。
    # webhook_env_key / routable も据置 (無効化 enabled=False で代替する)。
    for builtin in BUILTIN_CHANNELS:
        current = parsed.get(builtin.id)
        if current is None:
            errs.append(
                f"built-in channel '{builtin.id}' は削除できません (enabled=false で無効化)"
            )
            continue
        if current.webhook_env_key != builtin.webhook_env_key:
            errs.append(f"built-in channel '{builtin.id}': webhook_env_key は変更できません")
        if current.routable != builtin.routable:
            errs.append(f"built-in channel '{builtin.id}': routable は変更できません")
        # alert は緊急 push の最終チャンネル。push=False (保存のみ) にすると全緊急通知が
        # 無音化する footgun になるため不可 (通知再設計でも alert は常に push 対象)。
        if builtin.id == "alert" and not current.push:
            errs.append("built-in channel 'alert': push (Discord 配信) は無効化できません")

    # routing rules が参照中の channel は存在 + enabled が必要
    for ref in sorted(rule_channels):
        current = parsed.get(ref)
        if current is None:
            errs.append(f"channel '{ref}' は routing rules が参照中のため削除できません")
        elif not current.enabled:
            errs.append(f"channel '{ref}' は routing rules が参照中のため無効化できません")

    return errs


def resolve_webhooks(
    builtin_env: Mapping[str, str],
    *,
    db_path: Path | None = None,
    dotenv_path: Path = _DEFAULT_DOTENV_PATH,
) -> dict[str, str]:
    """有効チャンネルの channel id → webhook URL を解決する。

    Args:
        builtin_env: built-in 6 チャンネルの env 値 (``AppConfig.discord_webhooks``)。
            pydantic-settings が .env 込みで読んだ値を尊重し、built-in の解決を
            旧実装と完全同一に保つ。
        db_path: config_store の SQLite path (テスト用、production は None=PG)。
        dotenv_path: custom channel の env key を引く .env (環境変数が優先)。

    Returns:
        enabled なチャンネルのみの dict。URL 未設定は空文字 (旧挙動と同じく
        呼び出し側で publisher 生成を skip / fallback する)。
    """
    channels = [c for c in load_channels(db_path=db_path) if c.enabled]
    needs_dotenv = any(c.webhook_env_key not in _BUILTIN_ID_BY_ENV_KEY for c in channels)
    dotenv_vals: Mapping[str, str | None] = {}
    if needs_dotenv:
        try:
            from dotenv import dotenv_values

            dotenv_vals = dotenv_values(dotenv_path)
        except Exception as e:  # noqa: BLE001 — .env 不在/破損でも環境変数で解決を続行
            _log.warning("channel_registry_dotenv_load_failed", error=str(e))

    resolved: dict[str, str] = {}
    for ch in channels:
        builtin_id = _BUILTIN_ID_BY_ENV_KEY.get(ch.webhook_env_key)
        if builtin_id is not None and builtin_id in builtin_env:
            resolved[ch.id] = builtin_env[builtin_id]
            continue
        url = os.environ.get(ch.webhook_env_key) or dotenv_vals.get(ch.webhook_env_key) or ""
        resolved[ch.id] = url.strip()
    return resolved
