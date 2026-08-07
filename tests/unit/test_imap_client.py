"""src.tools.imap_client のテスト (Phase 2)。

実 IMAP サーバには接続せず、imaplib を unittest.mock で差し替えてフローを検証する。
"""

from __future__ import annotations

import re
from email.message import EmailMessage as StdEmailMessage
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.tools.imap_client import (
    DEFAULT_URL_PATTERN,
    EmailMessage,
    ImapAuthError,
    ImapClient,
    ImapConnectionError,
    _decode_header,
    _extract_bodies,
    _extract_urls,
    _matches_any,
    _parse_date,
)

# ---------- 単体ヘルパ ----------


class TestDecodeHeader:
    def test_returns_empty_for_empty(self) -> None:
        assert _decode_header("") == ""

    def test_decodes_plain_ascii(self) -> None:
        assert _decode_header("Hello") == "Hello"

    def test_decodes_iso2022jp(self) -> None:
        encoded = "=?ISO-2022-JP?B?GyRCJU8lbSE8GyhC?="
        result = _decode_header(encoded)
        assert "ハロー" in result


class TestParseDate:
    def test_returns_now_for_empty(self) -> None:
        dt = _parse_date("")
        assert dt.tzinfo is not None

    def test_parses_rfc2822(self) -> None:
        dt = _parse_date("Mon, 01 Jan 2024 12:00:00 +0900")
        assert dt.year == 2024
        assert dt.tzinfo is not None

    def test_returns_now_for_garbage(self) -> None:
        dt = _parse_date("not a date")
        assert dt.tzinfo is not None


class TestMatchesAny:
    def test_no_filters_matches_anything(self) -> None:
        assert _matches_any("anything", None)
        assert _matches_any("anything", [])

    def test_substring_match(self) -> None:
        assert _matches_any("noreply@x.ai", ["x.ai"])

    def test_case_insensitive(self) -> None:
        assert _matches_any("Grok Reports", ["grok"])

    def test_no_match(self) -> None:
        assert not _matches_any("hello", ["world"])


class TestExtractUrls:
    def test_extracts_from_text(self) -> None:
        text = "Visit https://grok.com/share/abc and https://example.com/x?q=1"
        urls = _extract_urls(text, "", DEFAULT_URL_PATTERN)
        assert "https://grok.com/share/abc" in urls
        assert "https://example.com/x?q=1" in urls

    def test_deduplicates(self) -> None:
        urls = _extract_urls(
            "https://x.com/y https://x.com/y",
            "<a href='https://x.com/y'>foo</a>",
            DEFAULT_URL_PATTERN,
        )
        assert urls == ["https://x.com/y"]

    def test_handles_html(self) -> None:
        html = '<a href="https://grok.com/abc">link</a>'
        urls = _extract_urls("", html, DEFAULT_URL_PATTERN)
        assert "https://grok.com/abc" in urls

    def test_strips_trailing_punctuation(self) -> None:
        urls = _extract_urls("see https://x.com/a.", "", DEFAULT_URL_PATTERN)
        assert urls == ["https://x.com/a"]

    def test_custom_pattern(self) -> None:
        pat = re.compile(r"https://grok\.com/[a-z0-9/]+")
        urls = _extract_urls(
            "https://grok.com/abc https://other.com/x",
            "",
            pat,
        )
        assert urls == ["https://grok.com/abc"]


class TestExtractBodies:
    def test_plain_only(self) -> None:
        msg = StdEmailMessage()
        msg.set_content("Plain text")
        text, html = _extract_bodies(msg)
        assert "Plain text" in text
        assert html == ""

    def test_multipart_text_and_html(self) -> None:
        msg = StdEmailMessage()
        msg["Subject"] = "Test"
        msg.set_content("Plain version")
        msg.add_alternative("<p>HTML version</p>", subtype="html")
        text, html = _extract_bodies(msg)
        assert "Plain version" in text
        assert "<p>HTML version</p>" in html


# ---------- ImapClient (mocked imaplib) ----------


class TestImapClientLifecycle:
    @pytest.mark.asyncio
    async def test_requires_credentials(self) -> None:
        with pytest.raises(ValueError, match="user"):
            ImapClient(user="", password="")

    @pytest.mark.asyncio
    async def test_login_failure_raises_auth_error(self) -> None:
        with patch("src.tools.imap_client.imaplib.IMAP4_SSL") as MockIMAP:  # noqa: N806
            instance = MagicMock()
            from imaplib import IMAP4

            instance.login.side_effect = IMAP4.error("invalid creds")
            MockIMAP.return_value = instance
            client = ImapClient(host="imap.example.com", user="u", password="p")
            with pytest.raises(ImapAuthError):
                await client.connect()

    @pytest.mark.asyncio
    async def test_connection_error_raises_connection_error(self) -> None:
        with patch("src.tools.imap_client.imaplib.IMAP4_SSL") as MockIMAP:  # noqa: N806
            MockIMAP.side_effect = OSError("DNS failure")
            client = ImapClient(host="bad.example.com", user="u", password="p")
            with pytest.raises(ImapConnectionError):
                await client.connect()


class TestImapClientFetch:
    @pytest.mark.asyncio
    async def test_no_messages_returns_empty(self) -> None:
        with patch("src.tools.imap_client.imaplib.IMAP4_SSL") as MockIMAP:  # noqa: N806
            instance = MagicMock()
            instance.uid.return_value = ("OK", [b""])
            MockIMAP.return_value = instance
            async with ImapClient(user="u", password="p") as client:
                result = await client.fetch_unread()
            assert result == []

    @pytest.mark.asyncio
    async def test_filters_by_sender_and_subject(self) -> None:
        # 2 通: 1 通は filter ヒット、もう 1 通は ヒットしない
        msg_match = (
            b"From: noreply@x.ai\r\n"
            b"Subject: Your Grok report is ready\r\n"
            b"Date: Mon, 01 Jan 2024 12:00:00 +0900\r\n"
            b"Content-Type: text/plain\r\n"
            b"\r\n"
            b"Visit https://grok.com/share/abc to view\r\n"
        )
        msg_skip = (
            b"From: someone@example.com\r\n"
            b"Subject: Newsletter\r\n"
            b"Date: Mon, 01 Jan 2024 12:00:00 +0900\r\n"
            b"Content-Type: text/plain\r\n"
            b"\r\n"
            b"unrelated\r\n"
        )

        with patch("src.tools.imap_client.imaplib.IMAP4_SSL") as MockIMAP:  # noqa: N806
            instance = MagicMock()

            def uid_handler(*args: Any, **kwargs: Any) -> tuple[str, list[Any]]:
                command = args[0]
                if command == "SEARCH":
                    return ("OK", [b"1 2"])
                if command == "FETCH":
                    uid = args[1]
                    if uid == "1":
                        return ("OK", [(b"1 (RFC822 {n}", msg_match)])
                    return ("OK", [(b"2 (RFC822 {n}", msg_skip)])
                if command == "STORE":
                    return ("OK", [b""])
                return ("OK", [b""])

            instance.uid.side_effect = uid_handler
            MockIMAP.return_value = instance

            async with ImapClient(user="u", password="p") as client:
                results = await client.fetch_unread(
                    sender_filters=["x.ai"],
                    subject_filters=["Grok"],
                )

        assert len(results) == 1
        assert isinstance(results[0], EmailMessage)
        assert results[0].uid == "1"
        assert "https://grok.com/share/abc" in results[0].extracted_urls

    @pytest.mark.asyncio
    async def test_mark_as_read_sends_store_flag(self) -> None:
        msg = (
            b"From: x@x.ai\r\n"
            b"Subject: Grok\r\n"
            b"Content-Type: text/plain\r\n"
            b"\r\n"
            b"https://grok.com/share/x\r\n"
        )

        seen_calls: list[tuple[Any, ...]] = []
        with patch("src.tools.imap_client.imaplib.IMAP4_SSL") as MockIMAP:  # noqa: N806
            instance = MagicMock()

            def uid_handler(*args: Any, **kwargs: Any) -> tuple[str, list[Any]]:
                seen_calls.append(args)
                command = args[0]
                if command == "SEARCH":
                    return ("OK", [b"42"])
                if command == "FETCH":
                    return ("OK", [(b"42 (RFC822 {n}", msg)])
                if command == "STORE":
                    return ("OK", [b""])
                return ("OK", [b""])

            instance.uid.side_effect = uid_handler
            MockIMAP.return_value = instance

            async with ImapClient(user="u", password="p") as client:
                await client.fetch_unread(
                    sender_filters=["x.ai"],
                    subject_filters=["Grok"],
                    mark_as_read=True,
                )

        store_calls = [c for c in seen_calls if c[0] == "STORE"]
        assert len(store_calls) == 1
        assert store_calls[0][1] == "42"
        assert "Seen" in str(store_calls[0])
