"""triage の判定を記録して、繰り越した記事で使い回す (2026-11-20、11-19 監査 D4 O1 の triage 部分)。

未見の記事は既読にならず次の run で再び選ばれ、``_filter_by_triage`` が全件を判定し直す。
triage の呼び出しの 32〜35% がこの繰り返しだった (RSS の処理量の飽和の一因)。

鍵 = sha256(記事 URL・モデル・**プロンプト全文**)。プロンプトは題・材料 (先行取得の本文・遮断文の
除去の有無)・基準 (rubric・PIR) を全部含み、日時を含まないので、どれかが変われば鍵が変わって
自動で使われなくなる。``TTL_HOURS`` を過ぎた記録は使わない。LLM の失敗で medium に倒した判定は
記録しない。救済・取り込みのヒントは判定の後段なので対象外 (毎回やり直す)。

旗 ``TRIAGE_DECISION_REUSE=0|shadow|1`` (コードの既定 0、compose の既定 shadow)。
- shadow: 毎回 LLM で判定し、記録があれば一致したかをログ ``triage_reuse_shadow`` に出す
  (使い回した場合に判定がどれだけ変わるかの測定)。判定は記録する。
- 1: 有効な記録があれば LLM を呼ばずに使う (ログ ``triage_reused``)。
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from typing import Literal

from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository
from src.tools.article_triage import TriageDecision

_log = get_logger(__name__)

FLAG = "TRIAGE_DECISION_REUSE"
TTL_HOURS = 24
#: これより古い記録は書き込みのついでに消す (TTL の 2 倍)
PURGE_AFTER_HOURS = 48

Mode = Literal["off", "shadow", "on"]


def reuse_mode() -> Mode:
    raw = os.environ.get(FLAG, "0").strip().lower()
    if raw == "shadow":
        return "shadow"
    if raw in {"1", "on", "true"}:
        return "on"
    return "off"


def decision_key(url: str, model: str, prompt: str) -> str:
    digest = hashlib.sha256()
    for part in (url, model, prompt):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


class TriageDecisionCache:
    """判定の記録の読み書き (失敗は握って「記録なし」に倒す — triage を止めない)。"""

    def __init__(self, repo: RunHistoryRepository, *, now: datetime | None = None) -> None:
        self._repo = repo
        self._now = now or datetime.now(UTC)

    def get(self, key: str) -> TriageDecision | None:
        since = (self._now - timedelta(hours=TTL_HOURS)).isoformat()
        try:
            with self._repo._connect() as conn:  # noqa: SLF001
                row = conn.execute(
                    "SELECT importance, reason FROM triage_decision_cache"
                    " WHERE decision_key = ? AND decided_at >= ?",
                    (key, since),
                ).fetchone()
        except Exception as e:  # noqa: BLE001
            _log.warning("triage_reuse_read_failed", error_type=type(e).__name__)
            return None
        if row is None:
            return None
        return TriageDecision(importance=row["importance"], reason=str(row["reason"] or ""))

    def put(self, key: str, decision: TriageDecision) -> None:
        if decision.error:
            return
        try:
            with self._repo._connect() as conn:  # noqa: SLF001
                conn.execute(
                    "INSERT INTO triage_decision_cache (decision_key, importance, reason,"
                    " decided_at) VALUES (?, ?, ?, ?) ON CONFLICT(decision_key) DO UPDATE SET"
                    " importance = excluded.importance, reason = excluded.reason,"
                    " decided_at = excluded.decided_at",
                    (key, decision.importance, decision.reason, self._now.isoformat()),
                )
        except Exception as e:  # noqa: BLE001
            _log.warning("triage_reuse_write_failed", error_type=type(e).__name__)

    def purge(self) -> None:
        cutoff = (self._now - timedelta(hours=PURGE_AFTER_HOURS)).isoformat()
        try:
            with self._repo._connect() as conn:  # noqa: SLF001
                conn.execute("DELETE FROM triage_decision_cache WHERE decided_at < ?", (cutoff,))
        except Exception as e:  # noqa: BLE001
            _log.warning("triage_reuse_purge_failed", error_type=type(e).__name__)


def open_cache(mode: Mode) -> TriageDecisionCache | None:
    """旗が 0 なら None (DB に触れない)。"""
    if mode == "off":
        return None
    cache = TriageDecisionCache(RunHistoryRepository())
    cache.purge()
    return cache


__all__ = [
    "FLAG",
    "TTL_HOURS",
    "TriageDecisionCache",
    "decision_key",
    "open_cache",
    "reuse_mode",
]
