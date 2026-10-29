"""Grok の描画されなかったレポートの取り直しの上限と警報の抑制 (2026-10-29 P0-25)。

本文が画面に描かれないレポートは、メールを未読に残して毎時取り直す。描画の失敗が続くと
毎時の警報が最大 24 時間続き、その後メールは取得窓 (lookback) から黙って外れて失われる。
メール (IMAP UID) ごとに連続の失敗を数え、次の 3 つを行う。

- 最初の失敗だけ警報にする (繰り返しは log のみ)
- 上限回数に達したら取り直しをやめ、その旨を 1 回だけ通知する
- 取得窓から外れる直前に、まだ読めていないメールを 1 回だけ警告する

記録の読み書きが失敗しても取得は止めず、警報を出す側に倒す (黙って失わない)。
旗 ``GROK_RETRY_CAP`` (0 = 無効 = 従来どおり毎回取り直し・毎回警報)。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from src.logging_config import get_logger
from src.storage.repo_grok_attempts import GrokAttempt
from src.tools.imap_client import EmailMessage

_log = get_logger(__name__)

GROK_RETRY_CAP_ENV = "GROK_RETRY_CAP"
DEFAULT_GROK_RETRY_CAP = 3
#: 取得窓から外れる何分前から警告するか (毎時の run で 2 回以上の機会を残す)
GROK_EXPIRY_WARN_LEAD_MINUTES = 180
UNRENDERED_REASON = "report_not_rendered"


class GrokAttemptStore(Protocol):
    def get_grok_attempt(self, email_uid: str) -> GrokAttempt | None: ...

    def record_grok_failure(
        self, email_uid: str, received_at: datetime, reason: str, *, when: datetime | None = None
    ) -> GrokAttempt: ...

    def mark_grok_capped_notified(self, email_uid: str, *, when: datetime) -> None: ...

    def mark_grok_expiry_warned(self, email_uid: str, *, when: datetime) -> None: ...

    def clear_grok_attempt(self, email_uid: str) -> None: ...


def grok_retry_cap() -> int:
    """``GROK_RETRY_CAP`` (既定 3、0 以下 = 無効)。数値でなければ既定。"""
    raw = os.environ.get(GROK_RETRY_CAP_ENV, "").strip()
    if not raw:
        return DEFAULT_GROK_RETRY_CAP
    try:
        return max(int(raw), 0)
    except ValueError:
        return DEFAULT_GROK_RETRY_CAP


class GrokRetryTracker:
    """メールごとの描画失敗の連続回数を数え、取り直し・警報を抑える。"""

    def __init__(
        self,
        store: GrokAttemptStore | None,
        *,
        cap: int,
        lookback_minutes: int | None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._cap = cap
        self._lookback = lookback_minutes
        self._clock = clock
        self.gave_up_count = 0
        self.expiring_count = 0
        self.skipped_count = 0
        self.store_errors = 0

    @property
    def is_active(self) -> bool:
        return self._store is not None and self._cap > 0

    def begin_run(self) -> None:
        self.gave_up_count = 0
        self.expiring_count = 0
        self.skipped_count = 0
        self.store_errors = 0

    def _get(self, uid: str) -> GrokAttempt | None:
        if not self.is_active or self._store is None:
            return None
        try:
            return self._store.get_grok_attempt(uid)
        except Exception as e:  # noqa: BLE001 — 記録が読めなくても取得は続ける
            self._store_failed("get", e)
            return None

    def _store_failed(self, op: str, error: Exception) -> None:
        self.store_errors += 1
        _log.warning("grok_retry_store_failed", op=op, error=type(error).__name__)

    def should_skip(self, msg: EmailMessage) -> bool:
        """上限に達したメールは取りにいかない。"""
        attempt = self._get(msg.uid)
        if attempt is None or attempt.fail_count < self._cap:
            return False
        self.skipped_count += 1
        _log.info("grok_retry_capped_skip", email_uid=msg.uid, fail_count=attempt.fail_count)
        return True

    def check_expiry(self, msg: EmailMessage) -> None:
        """失敗中のメールが取得窓から外れる直前なら、1 回だけ警告として数える。"""
        if self._lookback is None:
            return
        attempt = self._get(msg.uid)
        if attempt is None or attempt.expiry_warned_at:
            return
        expires_at = msg.received_at + timedelta(minutes=self._lookback)
        now = self._clock()
        if now < expires_at - timedelta(minutes=GROK_EXPIRY_WARN_LEAD_MINUTES):
            return
        # 印を付けられなくても警告は出す (重複しても黙って失うよりよい)
        self._mark("mark_grok_expiry_warned", msg.uid, now)
        self.expiring_count += 1
        _log.warning(
            "grok_report_expiring_unread", email_uid=msg.uid, fail_count=attempt.fail_count
        )

    def on_unrendered(self, msg: EmailMessage) -> bool:
        """描画失敗を数える。警報にすべきか (= 最初の失敗か、記録できなかったか) を返す。"""
        if not self.is_active or self._store is None:
            return True
        try:
            attempt = self._store.record_grok_failure(
                msg.uid, msg.received_at, UNRENDERED_REASON, when=self._clock()
            )
        except Exception as e:  # noqa: BLE001 — 記録できないときは警報側に倒す
            self._store_failed("record", e)
            return True
        if attempt.fail_count >= self._cap and not attempt.capped_notified_at:
            self._mark("mark_grok_capped_notified", msg.uid, self._clock())
            self.gave_up_count += 1
        return attempt.fail_count == 1

    def on_success(self, uid: str) -> None:
        if not self.is_active or self._store is None:
            return
        try:
            self._store.clear_grok_attempt(uid)
        except Exception as e:  # noqa: BLE001 — 消し損ねても次の成功で消える
            self._store_failed("clear", e)

    def _mark(self, method: str, uid: str, when: datetime) -> None:
        if self._store is None:
            return
        try:
            getattr(self._store, method)(uid, when=when)
        except Exception as e:  # noqa: BLE001
            self._store_failed(method, e)
