"""Grok の描画されなかったレポートの通知の抑制 (2026-10-29 P0-25)。

本文が画面に描かれないレポートは、メールを未読に残して毎時取り直す。**取り直しは止めない**
(回線が遅いだけの失敗は、窓が残る限り後の回で読める。実例: 3 回連続で失敗した後に回復)。
抑えるのは通知だけで、メール (IMAP UID) ごとに連続の失敗を数える。

- 最初の失敗で 1 回通知する
- その後は N 回目ごと (``GROK_UNRENDERED_SUMMARY_EVERY``、既定 6) に「まだ取れていない」を要約する
- 取得窓から外れる直前に、まだ読めていないメールを 1 回だけ警告する

記録の読み書きが失敗しても取得は止めず、通知する側に倒す (黙って失わない)。
``GROK_UNRENDERED_SUMMARY_EVERY=0`` で従来どおり毎回通知する。
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

GROK_SUMMARY_EVERY_ENV = "GROK_UNRENDERED_SUMMARY_EVERY"
DEFAULT_GROK_SUMMARY_EVERY = 6
#: 取得窓から外れる何分前から警告するか (毎時の run で 2 回以上の機会を残す)
GROK_EXPIRY_WARN_LEAD_MINUTES = 180
#: サーバの時間帯のずれ (日付境界の前倒し) に備える余裕。SINCE は日付単位で、サーバの
#: 日付が UTC より進んでいると境界が早まる。運用者の時間帯 (+9h) に合わせて早めに倒す
GROK_SINCE_TZ_MARGIN_MINUTES = 540
UNRENDERED_REASON = "report_not_rendered"


class GrokAttemptStore(Protocol):
    def get_grok_attempt(self, email_uid: str) -> GrokAttempt | None: ...

    def record_grok_failure(
        self, email_uid: str, received_at: datetime, reason: str, *, when: datetime | None = None
    ) -> GrokAttempt: ...

    def mark_grok_expiry_warned(self, email_uid: str, *, when: datetime) -> None: ...

    def clear_grok_attempt(self, email_uid: str) -> None: ...


def grok_summary_every() -> int:
    """``GROK_UNRENDERED_SUMMARY_EVERY`` (既定 6、0 以下 = 毎回通知)。数値でなければ既定。"""
    raw = os.environ.get(GROK_SUMMARY_EVERY_ENV, "").strip()
    if not raw:
        return DEFAULT_GROK_SUMMARY_EVERY
    try:
        return max(int(raw), 0)
    except ValueError:
        return DEFAULT_GROK_SUMMARY_EVERY


class GrokRetryTracker:
    """メールごとの描画失敗の連続回数を数え、取り直し・警報を抑える。"""

    def __init__(
        self,
        store: GrokAttemptStore | None,
        *,
        summary_every: int,
        lookback_minutes: int | None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._every = summary_every
        self._lookback = lookback_minutes
        self._clock = clock
        self.summary_count = 0
        self.expiring_count = 0
        self.store_errors = 0

    @property
    def is_active(self) -> bool:
        return self._store is not None and self._every > 0

    def begin_run(self) -> None:
        self.summary_count = 0
        self.expiring_count = 0
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

    def check_expiry(self, msg: EmailMessage) -> None:
        """失敗中のメールが取得窓から外れる直前なら、1 回だけ警告として数える。"""
        if self._lookback is None:
            return
        attempt = self._get(msg.uid)
        if attempt is None or attempt.expiry_warned_at:
            return
        now = self._clock()
        if now < self._warn_from(msg):
            return
        # 印を付けられなくても警告は出す (重複しても黙って失うよりよい)
        self._mark("mark_grok_expiry_warned", msg.uid, now)
        self.expiring_count += 1
        _log.warning(
            "grok_report_expiring_unread", email_uid=msg.uid, fail_count=attempt.fail_count
        )

    def _warn_from(self, msg: EmailMessage) -> datetime:
        """警告を始める時刻。実際の窓は IMAP の ``SINCE`` (日付単位) で決まる。

        SINCE は ``(now - lookback)`` の**日付**以降だけを返すので、受け取った日 D のメールは
        ``now - lookback`` が D+1 の 0 時 (UTC) に達した時点で窓から外れる。サーバの時間帯の
        ずれ分と警告の先行時間だけ早めに倒す。基準は INTERNALDATE (SINCE と同じ)、無ければ Date。
        """
        assert self._lookback is not None
        base = (msg.internal_date or msg.received_at).astimezone(UTC)
        next_midnight = datetime.combine(base.date() + timedelta(days=1), datetime.min.time(), UTC)
        exit_at = next_midnight + timedelta(minutes=self._lookback)
        early = GROK_SINCE_TZ_MARGIN_MINUTES + GROK_EXPIRY_WARN_LEAD_MINUTES
        return exit_at - timedelta(minutes=early)

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
        if attempt.fail_count > 1 and attempt.fail_count % self._every == 0:
            self.summary_count += 1
            _log.warning(
                "grok_report_still_unrendered", email_uid=msg.uid, fail_count=attempt.fail_count
            )
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
