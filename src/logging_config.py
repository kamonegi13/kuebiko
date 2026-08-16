"""structlog ベースの構造化ログ基盤 (Phase 1.5: stdout 化)。

CLAUDE.md §3 のロギング方針:
- structlog で構造化 JSON ログを **stdout に出力** (12-Factor App)
- ファイル書き出しは行わない (Docker / OS が転送・ローテーションを担う)
- 履歴の検索性は SQLite (run_history テーブル) で確保

CLAUDE.md §4 のセキュリティ要件:
- 機密情報を含む可能性のあるログ値は ``SENSITIVE_KEYS`` にマッチした
  場合に自動マスク (二重防御; プライマリ防御は「機密値をログ呼び出しに渡さない」)

公開 API:
    ``get_logger(name)`` で名前付き ``BoundLogger`` を取得 (idempotent 初期化)。

出力先:
    - stdout: TTY なら ``ConsoleRenderer`` (色付き)、それ以外なら JSON Lines
    - これにより ``docker logs`` / ``uv run python -m src.main`` 双方で適切な
      レンダリング

ログレベル: 環境変数 ``LOG_LEVEL`` (default ``INFO``)。
"""

from __future__ import annotations

import logging
import os
import re
import sys
from datetime import UTC, datetime
from typing import Any

import structlog
from structlog.types import EventDict

# CLAUDE.md §4 で出力禁止と明示された値の格納先キー名。
# 大文字小文字を無視した部分一致で検出する。追加が必要になったらここに足す。
# 部分一致ゆえ ``input_tokens`` / ``email_count`` 等の数値テレメトリを巻き込むため、
# 値が数値のときはマスクしない (``_is_numeric_telemetry`` に根拠)。
SENSITIVE_KEYS: tuple[str, ...] = (
    "api_key",
    "token",
    "password",
    "secret",
    "authorization",
    "cookie",
    "webhook",  # discord webhook URL を漏らさない (CLAUDE.md §4)
    "email",  # メールアドレスを漏らさない (CLAUDE.md §4)
    "imap_user",  # IMAP アカウント (= 運用者の Gmail) を漏らさない
)

# マスク表示形式: 値の先頭 ``MASK_PREFIX_LEN`` 文字 + ``MASK_SUFFIX``
MASK_PREFIX_LEN = 4
MASK_SUFFIX = "***"

# 機密 URL を含む可能性のある外部 HTTP client logger は INFO で URL を吐くので
# WARNING まで抑制 (CLAUDE.md §4)。エラー時は WARNING 以上で出力される。
NOISY_HTTP_LOGGERS: tuple[str, ...] = (
    "httpx",
    "httpcore",
)

# 任意の文字列内に紛れた webhook / 認証付き URL を検出してマスクする (二重防御)。
# キー名マッチだけでは httpx の event="HTTP Request: GET https://..." のような
# 埋め込み URL を捕捉できないため、文字列値も regex で粗くマスクする。
_WEBHOOK_URL_RE = re.compile(
    r"https?://(?:discord(?:app)?\.com|hooks\.slack\.com|[^/\s]+)/api/webhooks/\S+",
    re.IGNORECASE,
)
_MASKED_WEBHOOK = "<masked-webhook-url>"
# 文字列値に紛れたメールアドレスもマスク (CLAUDE.md §4: メールアドレスはログ出力禁止)。
# キー名マッチだけでは subprocess の stdout 行などに混ざった生の email を捕捉できない。
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_MASKED_EMAIL = "<masked-email>"


def _mask_value(value: Any) -> str:
    """値を安全にマスクする。短い値・非文字列は完全マスク。"""
    if not isinstance(value, str) or len(value) <= MASK_PREFIX_LEN:
        return MASK_SUFFIX
    return value[:MASK_PREFIX_LEN] + MASK_SUFFIX


def _is_numeric_telemetry(value: Any) -> bool:
    """数値 (int / float / bool) か。True ならキー名が一致してもマスクしない。

    CLAUDE.md §4 の出力禁止項目 (API キー / トークン / パスワード / メールアドレス /
    IMAP 認証情報 / Discord webhook URL / 記事本文) はいずれも文字列であり、数値が
    機密になる経路が無い。よって型で切り分けても二重防御の強度は落ちない。

    背景 (2026-08-16): ``SENSITIVE_KEYS`` はキー名の部分一致で判定するため、
    ``token`` が ``input_tokens`` / ``max_tokens`` を、``email`` が ``email_count``
    を巻き添えにし、LLM のコスト計測に必要な実測値を 30 日で 44,281 件失っていた。
    キー名の allowlist にすると新しい数値キーが再び黙って消えるため、値の型で
    クラスごと塞ぐ。
    """
    return isinstance(value, (int, float))


def _mask_text(text: str) -> str:
    """文字列内に埋め込まれた webhook URL / メールアドレスをマスクする (二重防御)。"""
    return _EMAIL_RE.sub(_MASKED_EMAIL, _WEBHOOK_URL_RE.sub(_MASKED_WEBHOOK, text))


def _mask_event_dict(d: EventDict) -> EventDict:
    """``EventDict`` 内の機密キー + 文字列値中の webhook URL を再帰的にマスクする。"""
    masked: dict[str, Any] = {}
    for key, value in d.items():
        key_lc = key.lower()
        if any(s in key_lc for s in SENSITIVE_KEYS):
            masked[key] = value if _is_numeric_telemetry(value) else _mask_value(value)
        elif isinstance(value, str):
            masked[key] = _mask_text(value)
        elif isinstance(value, dict):
            masked[key] = _mask_event_dict(value)
        elif isinstance(value, list):
            masked[key] = [
                _mask_event_dict(item)
                if isinstance(item, dict)
                else _mask_text(item)
                if isinstance(item, str)
                else item
                for item in value
            ]
        else:
            masked[key] = value
    return masked


def mask_sensitive_processor(_: Any, __: str, event_dict: EventDict) -> EventDict:
    """structlog プロセッサ: 機密キーを検出してマスクする。"""
    return _mask_event_dict(event_dict)


def add_iso_utc_timestamp(_: Any, __: str, event_dict: EventDict) -> EventDict:
    """ISO 8601 UTC のタイムスタンプを ``timestamp`` キーに付与する。"""
    event_dict["timestamp"] = datetime.now(UTC).isoformat(timespec="milliseconds")
    return event_dict


def _resolve_log_level() -> int:
    name = os.environ.get("LOG_LEVEL", "INFO").upper()
    return getattr(logging, name, logging.INFO)


def _is_tty() -> bool:
    """stdout が対話端末かどうか (色付け判定)。"""
    return sys.stdout.isatty()


def configure_logging() -> None:
    """structlog + stdlib logging を初期化する。Idempotent。

    出力先は stdout のみ。Docker / OS のログ収集機構に流す前提 (12-Factor)。
    """
    log_level = _resolve_log_level()

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        add_iso_utc_timestamp,
        structlog.processors.CallsiteParameterAdder(
            parameters={
                structlog.processors.CallsiteParameter.MODULE,
                structlog.processors.CallsiteParameter.FUNC_NAME,
                structlog.processors.CallsiteParameter.LINENO,
            },
        ),
        # マスクは出力直前 (情報を集めた後にマスクすることで取りこぼしを減らす)
        mask_sensitive_processor,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # stdout ハンドラ: TTY なら ConsoleRenderer、それ以外は JSON Lines
    handler = logging.StreamHandler(sys.stdout)
    if _is_tty():
        renderer: structlog.types.Processor = structlog.dev.ConsoleRenderer(colors=True)
    else:
        renderer = structlog.processors.JSONRenderer()
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                renderer,
            ],
        ),
    )

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(log_level)

    # httpx / httpcore は INFO で URL を含む詳細を吐くため WARNING に抑制 (§4)
    for name in NOISY_HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


_configured = False


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """名前付き ``BoundLogger`` を取得。初回呼び出しで自動的に初期化される。"""
    global _configured
    if not _configured:
        configure_logging()
        _configured = True
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
