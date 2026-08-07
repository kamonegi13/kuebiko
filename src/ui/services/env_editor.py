"""``.env`` ファイルの key=value 編集サービス (Phase 1.5)。

CLAUDE.md §12: シークレットマスク、編集前バックアップ、atomic write。

設計:
- 既存の行構造 (コメント・空行) を保持
- 既知キーのみ更新可、未知キーは追加しない (allowlist)
- 値はクオートしない (現状の .env と一致)
- ``mask_env_value`` で表示用にマスク (logging と同形式)
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from pathlib import Path

from src.logging_config import get_logger

_log = get_logger(__name__)

# ``.env`` で許可する非チャンネル系キー (現状の .env.example に列挙されているもの)。
# 表示順 = UI での表示順。チャンネル webhook キーは channel registry から動的に
# 合成する (C2: チャンネル追加 → 設定画面の webhook 欄が自動で増える)。
BASE_ENV_KEYS: tuple[str, ...] = (
    "IMAP_HOST",
    "IMAP_PORT",
    "IMAP_USER",
    "IMAP_PASSWORD",
    "OLLAMA_BASE_URL",
    # モデル選択は UI「設定 → モデル」タブ (DB 版履歴) で行う。.env にはモデルスロットを持たない
    # (bootstrap 既定は model_tiers.BUILTIN_MODEL_TIERS)。ここは Ollama 接続 URL のみ。
    "MOBILE_TUNNEL_DEFAULT_ENABLED",
    # 外部 LLM (§4 改訂 2026-07-18)。UI「設定 → モデル」タブからも編集できる
    # (config は request ごとに .env を再読込するため保存は即時反映・再起動不要)。
    "ANTHROPIC_API_KEY",
    # Claude Code サブスクの長期トークン (claude setup-token で発行、sidecar bridge が使用)。
    # 未設定なら bridge は CLI 既定認証 (旧ホスト方式のログイン状態) に fallback。
    "CLAUDE_CODE_OAUTH_TOKEN",
    "LOG_LEVEL",
    "TIMEZONE",
    # 本文取得の User-Agent (2026-07-27)。UA 自己修復ジョブ (ua-health-check) が
    # canary 検証のうえ自動更新する。手動でも「設定 → システム」から編集可。
    # 未設定なら content_extractor の現行既定 (Chrome 安定版) を使う。
    "CONTENT_EXTRACTOR_USER_AGENT",
)

# 接続先ライフサイクルに従うキー (未設定時は .env 画面に枠を出さない — 登録は
# 「設定 → モデル」の接続先パネルから。書込許可 allowlist には常在)。
_CONNECTION_KEYS: frozenset[str] = frozenset({"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"})

# built-in 6 チャンネル webhook キーの人向け説明 (運用知見を含む curated 文。
# custom channel はレジストリの label から自動生成する)。
_CHANNEL_KEY_DESCRIPTIONS: dict[str, str] = {
    "DISCORD_WEBHOOK_ALERT": (
        "[alert] 即応 ch の投稿先 URL。KEV/0day 実環境悪用、日本標的+APT 等の即応事象。"
    ),
    "DISCORD_WEBHOOK_BRIEF": (
        "[brief] 朝読 ch の投稿先 URL。RSS 由来 high/medium の通常記事 (アドバイザリ/速報)。"
    ),
    "DISCORD_WEBHOOK_WATCH": (
        "[watch] 観察 ch の投稿先 URL。まとめ/チュートリアル/論説/プレスリリース/研究、"
        "信頼性低事象。"
    ),
    "DISCORD_WEBHOOK_OPS": ("[ops] 運用通知 ch の投稿先 URL。死活通知・スケジューラ起動失敗等。"),
    "DISCORD_WEBHOOK_JAPAN_WATCH": (
        "[japan_watch] 日本関連弱信号 ch の投稿先 URL。"
        "日本企業/政府/重要インフラへの言及、JPCERT/NISC 発信等の早期察知用。"
        "空のときは即応チャンネルに自動で振り替えます。"
    ),
}

# 非チャンネル系キーの人向け説明 (UI で表示)
BASE_ENV_KEY_DESCRIPTIONS: dict[str, str] = {
    "IMAP_HOST": "Grok 通知メール受信用 IMAP サーバ (既定: imap.gmail.com)。",
    "IMAP_PORT": "IMAP ポート番号 (既定: 993、IMAPS)。",
    "IMAP_USER": "IMAP ログイン ID (Gmail メールアドレス)。",
    "IMAP_PASSWORD": (
        "IMAP のアプリパスワード (シークレット)。Gmail の場合は 2 段階認証アプリパスワードを使用。"
    ),
    "OLLAMA_BASE_URL": (
        "Ollama サーバの URL。Docker から見たホストの場合は "
        "http://host.docker.internal:11434 になる。"
    ),
    "MOBILE_TUNNEL_DEFAULT_ENABLED": (
        "外出先閲覧用の Cloudflare tunnel を、アプリの初回起動時に自動で"
        "立ち上げるかどうかの既定値。"
        "1 = 既定で有効 (起動時に自動で tunnel を起動し、Discord の運用 ch に URL を通知)、"
        "0 = 既定で無効。"
        "効果があるのは初回起動時のみで、それ以降はスケジュール管理画面での切替状態を優先する。"
    ),
    "ANTHROPIC_API_KEY": (
        "外部 LLM (Anthropic) の API キー (シークレット)。空 = 外部 LLM 無効 (ローカルのみ)。"
        "設定すると「設定 → モデル」タブで anthropic:claude-* をティアに割当できる。即時反映。"
    ),
    "CLAUDE_CODE_OAUTH_TOKEN": (
        "Claude Code サブスクの長期トークン (シークレット)。`claude setup-token` で発行し"
        "「設定 → モデル」の Claude Code カードから貼付。sidecar bridge の認証に使う。"
    ),
    "LOG_LEVEL": "ログレベル (DEBUG / INFO / WARNING / ERROR)。本番は INFO。",
    "TIMEZONE": "アプリ全体のタイムゾーン (既定: Asia/Tokyo)。スケジューラの cron 解釈に影響。",
    "CONTENT_EXTRACTOR_USER_AGENT": (
        "本文取得の User-Agent。空 = 現行既定 (Chrome 安定版)。UA 自己修復ジョブが "
        "canary 検証のうえ古くなった UA を自動更新する。通常は空のままでよい。"
    ),
}

# 非チャンネル系のシークレットキー (チャンネル webhook キーは全件シークレット扱い)。
# IMAP_USER (= 運用者の Gmail) と IMAP_HOST は厳密には secret ではないが、外部公開の
# readonly instance (GET /api/v1/config) で平文露出すると個人メール + メールホストが攻撃者に
# 渡り IMAP アカウント標的化に繋がる (security-review H2)。そのため secret と同じく
# レスポンスでマスクし、空送信時は既存値を保持する経路に乗せる。
BASE_SECRET_ENV_KEYS: tuple[str, ...] = (
    "IMAP_PASSWORD",
    "IMAP_USER",
    "IMAP_HOST",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
)


def _channel_webhook_keys() -> tuple[str, ...]:
    """レジストリの webhook_env_key を表示順 (order 含む定義順) で返す (重複除去)。"""
    from src.tools.channel_registry import load_channels

    return tuple(dict.fromkeys(c.webhook_env_key for c in load_channels()))


def _llm_endpoint_env_keys() -> tuple[str, ...]:
    """登録済み LLM 接続先の API キー env 名 (レジストリ駆動、channel webhook と同型)。"""
    from src.tools.llm_endpoints import load_llm_endpoints

    return tuple(e.key_env for e in load_llm_endpoints())


def allowed_env_keys() -> tuple[str, ...]:
    """``.env`` 編集を許可するキー (C2: チャンネル webhook / LLM 接続先はレジストリ駆動)。

    表示順 = チャンネル webhook 群 → 非チャンネル系 (従来と同じ並び) → LLM 接続先キー。
    """
    return _channel_webhook_keys() + BASE_ENV_KEYS + _llm_endpoint_env_keys()


def displayed_env_keys(env_values: dict[str, str]) -> tuple[str, ...]:
    """``.env`` 編集画面に**表示**するキー (書込許可とは別)。

    接続系キー (_CONNECTION_KEYS) は接続先パネル (モデルタブ) から登録する枠 —
    ユーザ定義接続先のキーと対称に、未設定時は枠ごと非表示にする (登録すると現れる)。
    書込許可 (allowed_env_keys) には常に含む — モデルタブの初回登録経路が使うため。
    """
    return tuple(
        k
        for k in allowed_env_keys()
        if not (k in _CONNECTION_KEYS and not (env_values.get(k) or "").strip())
    )


def env_key_descriptions() -> dict[str, str]:
    """各キーの人向け説明。custom channel 分はレジストリ label から自動生成。"""
    from src.tools.channel_registry import load_channels

    channels = load_channels()
    # fallback 先の表示名もレジストリ (SSoT) から解決する (id の直書きを避ける)。
    label_by_id = {c.id: (c.label or c.id) for c in channels}
    out: dict[str, str] = {}
    for ch in channels:
        curated = _CHANNEL_KEY_DESCRIPTIONS.get(ch.webhook_env_key)
        if curated is not None:
            out[ch.webhook_env_key] = curated
            continue
        fallback_note = (
            f"空のときは{label_by_id.get(ch.fallback, ch.fallback)}チャンネルに自動で振り替えます。"
            if ch.fallback
            else ""
        )
        label = ch.label or ch.id
        out[ch.webhook_env_key] = f"[{ch.id}] {label} ch の投稿先 URL。{fallback_note}"
    out.update(BASE_ENV_KEY_DESCRIPTIONS)
    return out


def secret_env_keys() -> tuple[str, ...]:
    """UI 表示でマスクするキー (チャンネル webhook 全件 + IMAP_PASSWORD + LLM 接続先キー)。"""
    return _channel_webhook_keys() + BASE_SECRET_ENV_KEYS + _llm_endpoint_env_keys()


_KV_LINE = re.compile(r"^([A-Z_][A-Z_0-9]*)\s*=\s*(.*)$")


class EnvEditError(Exception):
    """allowlist 外のキーを更新しようとした、または書き込みに失敗。"""


def parse_env(text: str) -> dict[str, str]:
    """key=value のみを dict にパース (コメント・空行は無視)。"""
    out: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        m = _KV_LINE.match(line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def mask_value(value: str, *, prefix_len: int = 4, suffix: str = "***") -> str:
    if not value:
        return ""
    if len(value) <= prefix_len:
        return suffix
    return value[:prefix_len] + suffix


def update_env(
    env_path: Path,
    updates: dict[str, str],
) -> None:
    """既存 ``.env`` の指定キーのみを更新する (atomic rename + bak)。

    既存の行構造 (コメント・空行) は保持する。allowlist 外のキーは EnvEditError。
    """
    allowed = allowed_env_keys()
    for key in updates:
        if key not in allowed:
            raise EnvEditError("編集が許可されていない設定項目です")

    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""

    lines = text.splitlines()
    seen: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        m = _KV_LINE.match(line)
        if m:
            key = m.group(1)
            if key in updates:
                new_lines.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        new_lines.append(line)

    # 既存行に存在しないキーは末尾に追加
    for key, val in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={val}")

    new_text = "\n".join(new_lines)
    if not new_text.endswith("\n"):
        new_text += "\n"

    # backup
    if env_path.exists():
        backup = env_path.with_suffix(env_path.suffix + ".bak")
        backup.write_text(text, encoding="utf-8")
        _log.info("env_backup", path=str(env_path), backup=str(backup))

    # atomic rename を試みる。Docker bind-mount された個別ファイルでは
    # ``OSError(errno=16, EBUSY)`` で失敗するため、その場合のみ直接書込に fallback。
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(env_path.parent),
        delete=False,
        prefix=f".{env_path.name}.",
        suffix=".tmp",
    ) as tmpf:
        tmpf.write(new_text)
        tmpf.flush()
        os.fsync(tmpf.fileno())
        tmp_path = Path(tmpf.name)
    try:
        os.replace(tmp_path, env_path)
    except OSError as e:
        # EBUSY (Linux 16) / ETXTBSY 等 bind-mount file の制約に該当する場合のみ
        # tmp を消して直接書き込み。読み込み途中の race は短時間 (数 ms) で許容。
        _log.warning(
            "env_atomic_rename_failed_fallback_to_direct_write",
            error=str(e),
            errno=e.errno,
        )
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        env_path.write_text(new_text, encoding="utf-8")
    _log.info("env_updated", path=str(env_path), keys=sorted(updates.keys()))
