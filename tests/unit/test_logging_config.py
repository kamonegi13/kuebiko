"""src.logging_config のテスト (Phase 1.5: stdout 化)。

機密情報マスクが優先テストカテゴリ (CLAUDE.md §4 のセキュリティ要件)。
出力先は stdout のみ (12-Factor) なので、capsys / capfd で検証する。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest
import structlog

import src.logging_config as lc
from src.logging_config import (
    MASK_SUFFIX,
    _mask_event_dict,
    _mask_value,
    get_logger,
    mask_sensitive_processor,
)

# ----------------- 単体: _mask_value -----------------


class TestMaskValue:
    def test_long_string_keeps_4char_prefix(self) -> None:
        assert _mask_value("abcdefghij") == "abcd***"

    def test_string_at_threshold_is_fully_masked(self) -> None:
        assert _mask_value("abcd") == MASK_SUFFIX
        assert _mask_value("abc") == MASK_SUFFIX
        assert _mask_value("a") == MASK_SUFFIX

    def test_empty_string_is_masked(self) -> None:
        assert _mask_value("") == MASK_SUFFIX

    @pytest.mark.parametrize("value", [12345, None, True, [1, 2, 3], {"x": 1}, 3.14])
    def test_non_string_value_is_fully_masked(self, value: object) -> None:
        assert _mask_value(value) == MASK_SUFFIX


# ----------------- 単体: _mask_event_dict -----------------


class TestMaskEventDict:
    def test_sensitive_key_is_masked(self) -> None:
        out = _mask_event_dict({"api_key": "1234567890abcdef"})
        assert out == {"api_key": "1234***"}

    def test_non_sensitive_key_is_unchanged(self) -> None:
        out = _mask_event_dict({"user_id": "1003783288", "url": "https://example.com"})
        assert out == {"user_id": "1003783288", "url": "https://example.com"}

    @pytest.mark.parametrize(
        "key",
        [
            "API_KEY",
            "Api_Key",
            "x_api_key",
            "SERVICE_OAUTH_TOKEN",
            "refresh_token",
            "ACCESS_TOKEN",
            "bearer_token",
            "password",
            "Password",
            "client_secret",
            "app_secret",
            "authorization",
            "Authorization",
            "cookie",
            "Cookie",
            "set_cookie",
            "discord_webhook_url",
            "WEBHOOK_PRIORITY",
        ],
    )
    def test_case_insensitive_partial_match(self, key: str) -> None:
        out = _mask_event_dict({key: "supersecretvalue123"})
        assert out[key] == "supe***"

    def test_nested_dict_is_recursively_masked(self) -> None:
        out = _mask_event_dict(
            {
                "request": {
                    "headers": {"Authorization": "Bearer abcdefgh"},
                    "url": "https://example.com/api",
                },
            },
        )
        assert out["request"]["headers"]["Authorization"] == "Bear***"
        assert out["request"]["url"] == "https://example.com/api"

    def test_list_of_dicts_is_recursively_masked(self) -> None:
        out = _mask_event_dict(
            {
                "calls": [
                    {"api_key": "1234567890"},
                    {"endpoint": "/v1/foo"},
                    "non-dict-item",
                ],
            },
        )
        assert out["calls"][0]["api_key"] == "1234***"
        assert out["calls"][1]["endpoint"] == "/v1/foo"
        assert out["calls"][2] == "non-dict-item"

    def test_short_sensitive_value_is_fully_masked(self) -> None:
        out = _mask_event_dict({"token": "abc"})
        assert out["token"] == MASK_SUFFIX

    def test_input_dict_is_not_mutated(self) -> None:
        original: dict[str, str] = {"api_key": "1234567890"}
        _mask_event_dict(original)
        assert original == {"api_key": "1234567890"}


# ----------------- 数値テレメトリの誤検知 (2026-08-16 回帰防止) -----------------


class TestNumericTelemetryIsNotMasked:
    """キー名の部分一致が数値テレメトリを巻き添えにしないことを固定する。

    背景: ``token`` が ``input_tokens`` / ``max_tokens`` を、``email`` が
    ``email_count`` をマスクし、LLM のコスト計測に必要な実測値を 30 日間で
    44,281 件失っていた。CLAUDE.md §4 の禁止項目 (API キー / トークン /
    パスワード / メールアドレス / IMAP 認証情報 / webhook URL / 記事本文) は
    すべて文字列であり、数値が機密になる経路が無いため型で切り分ける。
    """

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("input_tokens", 8366),
            ("output_tokens", 519),
            ("max_tokens", 4096),
            ("email_count", 64),
            ("prompt_token_count", 0),
            ("duration_seconds_per_token", 0.0098),
        ],
    )
    def test_numeric_value_under_sensitive_key_is_kept(self, key: str, value: object) -> None:
        assert _mask_event_dict({key: value}) == {key: value}

    def test_numeric_telemetry_survives_in_nested_dict(self) -> None:
        out = _mask_event_dict({"usage": {"input_tokens": 8366, "api_key": "1234567890"}})
        assert out["usage"]["input_tokens"] == 8366
        assert out["usage"]["api_key"] == "1234***"

    # --- ここから下は緩和してはいけない性質 (security pin) ---

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("token", "abcdef123456"),
            ("input_tokens", "abcdef123456"),
            ("api_key", "sk-1234567890"),
            ("imap_user", "operator-account"),
            ("email", "someone@example.com"),
        ],
    )
    def test_string_value_under_sensitive_key_is_still_masked(self, key: str, value: str) -> None:
        assert _mask_event_dict({key: value})[key].endswith(MASK_SUFFIX)

    def test_ollama_response_shape_is_fully_observable(self) -> None:
        """実際に失われていたログ形状が、そのまま観測できることを固定する。"""
        out = _mask_event_dict(
            {
                "event": "ollama_response",
                "model": "gemma4:26b",
                "duration_seconds": 7.124,
                "input_tokens": 8366,
                "output_tokens": 519,
                "output_chars": 604,
                "structured": True,
            },
        )
        assert out["input_tokens"] == 8366
        assert out["output_tokens"] == 519
        assert out["duration_seconds"] == 7.124


# ----------------- 単体: structlog プロセッサシム -----------------


class TestMaskSensitiveProcessor:
    def test_processor_returns_masked_event_dict(self) -> None:
        result = mask_sensitive_processor(None, "info", {"token": "longsecretvalue"})
        assert result == {"token": "long***"}

    def test_processor_handles_empty_dict(self) -> None:
        assert mask_sensitive_processor(None, "info", {}) == {}


# ----------------- 統合: get_logger / configure_logging (stdout 出力) -----------------


@pytest.fixture
def isolated_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """structlog / stdlib logging をリセットし TTY 判定を強制 OFF。"""
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    # 非 TTY 経由 = JSON Lines レンダラを使う
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    lc._configured = False
    structlog.reset_defaults()
    logging.getLogger().handlers.clear()
    yield
    structlog.reset_defaults()
    logging.getLogger().handlers.clear()
    lc._configured = False


class TestEndToEnd:
    def test_get_logger_returns_bound_logger(self, isolated_logging: None) -> None:
        log = get_logger("test")
        assert hasattr(log, "info")
        assert hasattr(log, "bind")

    def test_log_writes_to_stdout_with_masked_token(
        self,
        isolated_logging: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        log = get_logger("test_module")
        log.info("hello world", api_key="abcdefghij1234567890")

        captured = capsys.readouterr()
        # 元の値が stdout 全体に漏れていない
        assert "abcdefghij1234567890" not in captured.out
        # マスク後の値で記録されている
        assert "abcd***" in captured.out
        # JSON Lines にパースできる
        record = json.loads(captured.out.strip().splitlines()[-1])
        assert record["api_key"] == "abcd***"
        assert record["event"] == "hello world"
        assert record["logger"] == "test_module"
        assert "timestamp" in record
        assert "module" in record

    def test_authorization_header_in_nested_dict_is_masked(
        self,
        isolated_logging: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        log = get_logger("test")
        log.info(
            "request_made",
            request={"headers": {"Authorization": "Bearer abcdefgh"}},
        )
        captured = capsys.readouterr()
        assert "Bearer abcdefgh" not in captured.out
        assert "Bear***" in captured.out

    def test_log_level_respects_env(
        self,
        isolated_logging: None,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("LOG_LEVEL", "ERROR")
        log = get_logger("test")
        log.info("should_not_appear")
        log.error("should_appear", token="secret-token-value")

        captured = capsys.readouterr()
        assert "should_not_appear" not in captured.out
        assert "should_appear" in captured.out
        # マスクが ERROR レベルでも適用される
        assert "secret-token-value" not in captured.out
        assert "secr***" in captured.out

    def test_webhook_url_is_masked(
        self,
        isolated_logging: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        log = get_logger("test")
        log.info(
            "publishing",
            discord_webhook_url="https://discord.com/api/webhooks/123/abcsecrettoken",
        )
        captured = capsys.readouterr()
        # URL の token 部分が漏れない
        assert "abcsecrettoken" not in captured.out
        assert "https://discord.com/api/webhooks" not in captured.out  # マスクで前半 4 文字のみ

    def test_webhook_url_embedded_in_event_string_is_masked(
        self,
        isolated_logging: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """httpx が event='HTTP Request: GET https://discord.com/api/webhooks/...' を吐く
        ケースを再現。キー名マッチでは捕捉できないので URL regex で粗マスクされる。"""
        log = get_logger("test")
        log.info(
            "HTTP Request: GET https://discord.com/api/webhooks/123/abcsecrettoken HTTP/1.1",
        )
        captured = capsys.readouterr()
        assert "abcsecrettoken" not in captured.out
        assert "<masked-webhook-url>" in captured.out

    def test_webhook_url_in_nested_string_value_is_masked(
        self,
        isolated_logging: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """通常キー (event/url 等) に webhook URL が文字列として埋め込まれた場合もマスク。"""
        log = get_logger("test")
        log.info(
            "discord_post_ok",
            target_url="https://discord.com/api/webhooks/9999/longsecrettoken_xxxxxx",
        )
        captured = capsys.readouterr()
        assert "longsecrettoken_xxxxxx" not in captured.out
        assert "<masked-webhook-url>" in captured.out

    def test_httpx_logger_is_quiesced_to_warning(
        self,
        isolated_logging: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """httpx の INFO ログは出力されない (URL 漏洩防止)。WARNING は出る。"""
        get_logger("bootstrap")  # configure_logging を発火させる
        httpx_log = logging.getLogger("httpx")
        httpx_log.info(
            "HTTP Request: GET https://discord.com/api/webhooks/1/abcsecrettoken",
        )
        httpx_log.warning("connection failure")
        captured = capsys.readouterr()
        # INFO は抑制される
        assert "abcsecrettoken" not in captured.out
        assert "HTTP Request" not in captured.out
        # WARNING は出る
        assert "connection failure" in captured.out
