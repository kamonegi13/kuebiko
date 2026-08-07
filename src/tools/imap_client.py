"""IMAP クライアント (Phase 2: Grok 通知メール受信)。

stdlib ``imaplib`` を ``asyncio.to_thread`` でラップした非同期インタフェース。
追加依存ライブラリなし。Gmail / Outlook 等のアプリパスワード前提。

設計方針:
- 1 接続で fetch + flag 更新までを完結させる (短命接続)
- 機密情報 (パスワード、メール本文) はログに出さない
  → ID + ヘッダ抜粋のみ出力 (CLAUDE.md §4)
- メール本文から URL を正規表現で抽出
- 既読化はオプション (デバッグ時は off にできる)

参考:
- RFC 3501 (IMAP4rev1)
- imaplib: https://docs.python.org/3/library/imaplib.html
"""

from __future__ import annotations

import asyncio
import contextlib
import email
import email.header
import email.utils
import imaplib
import re
from datetime import UTC, datetime, timedelta
from email.message import Message
from types import TracebackType
from typing import Self

from pydantic import BaseModel, ConfigDict

from src.logging_config import get_logger

_log = get_logger(__name__)

# 一般的な URL 抽出パターン (HTTP/HTTPS、HTML 属性内の引用符 / 末尾の句読点を除去)
DEFAULT_URL_PATTERN = re.compile(
    r"https?://[^\s<>\"']+[^\s<>\"'.,;:!?)]",
    re.IGNORECASE,
)

# Gmail のデフォルト IMAP server
DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993

# 既定のメール取得上限
DEFAULT_MAX_MESSAGES = 50


# ---------- Models ----------


class EmailMessage(BaseModel):
    """1 通のメールから抽出した正規化情報。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    uid: str
    subject: str
    sender: str
    received_at: datetime
    body_text: str
    body_html: str
    extracted_urls: list[str]


class ImapError(Exception):
    """IMAP 操作の汎用エラー。"""


class ImapAuthError(ImapError):
    """ログイン失敗 (アプリパスワード不正など)。"""


class ImapConnectionError(ImapError):
    """接続失敗 (DNS / TCP / TLS)。"""


# ---------- Client ----------


class ImapClient:
    """非同期 IMAP クライアント。短命接続で fetch + flag 操作を行う。

    使い方::

        async with ImapClient(host, port, user, password) as client:
            messages = await client.fetch_unread(
                sender_filters=["@x.ai"],
                subject_filters=["Grok"],
                lookback_minutes=1440,
            )
    """

    def __init__(
        self,
        host: str = DEFAULT_IMAP_HOST,
        port: int = DEFAULT_IMAP_PORT,
        user: str = "",
        password: str = "",
        mailbox: str = "INBOX",
        timeout_seconds: float = 30.0,
    ) -> None:
        if not user or not password:
            raise ValueError("IMAP user / password は必須です")
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._mailbox = mailbox
        self._timeout = timeout_seconds
        self._conn: imaplib.IMAP4_SSL | None = None

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    # ----- connection lifecycle -----

    async def connect(self) -> None:
        try:
            self._conn = await asyncio.to_thread(self._connect_sync)
        except imaplib.IMAP4.error as e:
            raise ImapAuthError(f"IMAP ログイン失敗: {e}") from e
        except (OSError, TimeoutError) as e:
            raise ImapConnectionError(f"IMAP 接続失敗 ({self._host}:{self._port}): {e}") from e
        # imap_user キーは SENSITIVE_KEYS でマスクされる (運用者の Gmail を run_logs に残さない)
        _log.info("imap_connected", host=self._host, port=self._port, imap_user=self._user)

    def _connect_sync(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(self._host, self._port, timeout=self._timeout)
        conn.login(self._user, self._password)
        conn.select(self._mailbox)
        return conn

    async def close(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._close_sync)
            self._conn = None
            _log.info("imap_disconnected", host=self._host)

    def _close_sync(self) -> None:
        assert self._conn is not None
        try:
            self._conn.close()
        finally:
            self._conn.logout()

    # ----- fetch -----

    async def fetch_unread(
        self,
        *,
        sender_filters: list[str] | None = None,
        subject_filters: list[str] | None = None,
        lookback_minutes: int | None = None,
        max_messages: int = DEFAULT_MAX_MESSAGES,
        url_pattern: re.Pattern[str] | None = None,
        mark_as_read: bool = False,
        unseen_only: bool = True,
    ) -> list[EmailMessage]:
        """未読メールを取得し ``EmailMessage`` リストに変換する。

        フィルタ:
            - ``sender_filters``: From: にいずれかの substring を含む (大文字小文字無視)
            - ``subject_filters``: Subject: にいずれかの substring を含む
            - ``lookback_minutes``: 指定分以内に受信したメールのみ
            - ``unseen_only``: ``True`` (既定) のとき UNSEEN のみ。``False`` で
              既読も含めて取得 (テスト・初回キャッチアップ時に利用)
        """
        if self._conn is None:
            raise ImapError("connect() が完了していません")
        url_re = url_pattern or DEFAULT_URL_PATTERN
        try:
            uids = await asyncio.to_thread(
                self._search_uids,
                lookback_minutes,
                unseen_only,
            )
        except imaplib.IMAP4.error as e:
            raise ImapError(f"IMAP SEARCH 失敗: {e}") from e

        _log.info("imap_search_completed", count=len(uids), mailbox=self._mailbox)
        if not uids:
            return []

        results: list[EmailMessage] = []
        for uid in uids[:max_messages]:
            try:
                raw = await asyncio.to_thread(self._fetch_uid, uid)
            except imaplib.IMAP4.error as e:
                _log.warning("imap_fetch_failed", uid=uid, error=str(e))
                continue
            if raw is None:
                continue

            msg = email.message_from_bytes(raw)
            sender = _decode_header(msg.get("From", ""))
            subject = _decode_header(msg.get("Subject", ""))
            if not _matches_any(sender, sender_filters) or not _matches_any(
                subject, subject_filters
            ):
                continue

            received = _parse_date(msg.get("Date", ""))
            text, html = _extract_bodies(msg)
            urls = _extract_urls(text, html, url_re)

            results.append(
                EmailMessage(
                    uid=uid,
                    subject=subject,
                    sender=sender,
                    received_at=received,
                    body_text=text,
                    body_html=html,
                    extracted_urls=urls,
                ),
            )

            if mark_as_read:
                try:
                    await asyncio.to_thread(self._mark_seen, uid)
                except imaplib.IMAP4.error as e:
                    _log.warning("imap_mark_seen_failed", uid=uid, error=str(e))

        _log.info(
            "imap_fetch_completed",
            considered=len(uids),
            matched=len(results),
        )
        return results

    async def mark_seen(self, uids: list[str]) -> int:
        """指定 UID 群を ``\\Seen`` (既読) にする。失敗は warning で飲み込む。

        fetch_unread は BODY.PEEK で読むため取得しても既読化しない。処理 (Grok レポート
        抽出) が成功した後にこのメソッドで明示的に既読化することで、失敗メールを未読に
        残して再試行可能にしつつ、成功メールはインボックスを汚さない。返り値は既読化数。
        """
        marked = 0
        for uid in uids:
            try:
                await asyncio.to_thread(self._mark_seen, uid)
                marked += 1
            except imaplib.IMAP4.error as e:
                _log.warning("imap_mark_seen_failed", uid=uid, error=str(e))
        return marked

    # ----- internal sync helpers -----

    def _search_uids(
        self,
        lookback_minutes: int | None,
        unseen_only: bool = True,
    ) -> list[str]:
        assert self._conn is not None
        criteria: list[str] = []
        if unseen_only:
            criteria.append("UNSEEN")
        if lookback_minutes is not None:
            since = datetime.now(UTC) - timedelta(minutes=lookback_minutes)
            criteria.extend(["SINCE", since.strftime("%d-%b-%Y")])
        if not criteria:
            criteria.append("ALL")
        typ, data = self._conn.uid("SEARCH", None, *criteria)  # type: ignore[arg-type]
        if typ != "OK" or not data or not data[0]:
            return []
        decoded: list[str] = data[0].decode().split()
        return decoded

    def _fetch_uid(self, uid: str) -> bytes | None:
        assert self._conn is not None
        # BODY.PEEK[] を使うと FETCH しても \Seen フラグが立たない。
        # RFC822 (= BODY[]) は副作用で auto-mark-as-seen するため、
        # 処理失敗時に UNSEEN search から消えて再試行できなくなる問題があった。
        # 明示的に mark するのは _mark_seen() で別途実行する。
        typ, data = self._conn.uid("FETCH", uid, "(BODY.PEEK[])")
        if typ != "OK" or not data:
            return None
        for chunk in data:
            if isinstance(chunk, tuple) and len(chunk) >= 2 and isinstance(chunk[1], bytes):
                return chunk[1]
        return None

    def _mark_seen(self, uid: str) -> None:
        assert self._conn is not None
        self._conn.uid("STORE", uid, "+FLAGS", "(\\Seen)")


# ---------- module-level helpers ----------


def _decode_header(raw: str) -> str:
    """RFC 2047 エンコードヘッダをデコードする。"""
    if not raw:
        return ""
    parts = email.header.decode_header(raw)
    out: list[str] = []
    for value, charset in parts:
        if isinstance(value, bytes):
            try:
                out.append(value.decode(charset or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                out.append(value.decode("utf-8", errors="replace"))
        else:
            out.append(value)
    return "".join(out).strip()


def _parse_date(raw: str) -> datetime:
    if not raw:
        return datetime.now(UTC)
    try:
        dt: datetime = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _matches_any(haystack: str, needles: list[str] | None) -> bool:
    if not needles:
        return True
    h_lower = haystack.lower()
    return any(n.lower() in h_lower for n in needles)


def _extract_bodies(msg: Message) -> tuple[str, str]:
    """multipart メールから text/plain と text/html を取り出す。"""
    text_parts: list[str] = []
    html_parts: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if part.get_content_disposition() == "attachment":
                continue
            payload = _decode_payload(part)
            if payload is None:
                continue
            if ctype == "text/plain":
                text_parts.append(payload)
            elif ctype == "text/html":
                html_parts.append(payload)
    else:
        ctype = msg.get_content_type()
        payload = _decode_payload(msg)
        if payload is not None:
            if ctype == "text/html":
                html_parts.append(payload)
            else:
                text_parts.append(payload)

    return "\n".join(text_parts).strip(), "\n".join(html_parts).strip()


def _decode_payload(part: Message) -> str | None:
    raw = part.get_payload(decode=True)
    if raw is None or not isinstance(raw, bytes):
        return None
    charset = part.get_content_charset() or "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _extract_urls(text: str, html: str, url_re: re.Pattern[str]) -> list[str]:
    """text と HTML 双方から URL を抽出 (重複除去、登場順を保持)。"""
    seen: set[str] = set()
    out: list[str] = []
    for source in (text, html):
        for match in url_re.findall(source):
            if match not in seen:
                seen.add(match)
                out.append(match)
    return out
