"""Grok レポートの過去分の取り込み (一度だけの手動実行用、2026-11-18)。

11-15〜11-18 に Grok の画面の自動リンク化で JSONL が 1 行も読めず、要約が 0 件だった
(fetcher の ``_unwrap_jsonl_autolinks`` で修正)。その間のレポートを取り戻すための旗。

``GROK_BACKFILL=1`` のときだけ (既定 0 = 本番の挙動は不変):

- メールを ``GROK_BACKFILL_LOOKBACK_MINUTES`` (既定 7 日) 遡り、既読のメールも対象にする
- 投稿の鮮度 (24 時間) を「今」ではなく**レポートのメールの受信時刻**で測る
- ``GROK_BACKFILL_UNTIL`` (ISO 8601、必須) より後に受信したメールは扱わない (修正の後の
  レポートは通常の run が配信するので、取り戻しの側で分析専用に奪わない)
- 要約した投稿はすべて分析専用 (``articles.analysis_only='grok_backfill'``) にし、
  Discord へは流さない (古い速報を今さら配信しない)

本番コンテナの常駐の環境には入れない。``docker exec -e GROK_BACKFILL=1 ...`` で 1 回だけ流す
(手順は ``scripts/grok_backfill.py`` の docstring)。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

DEFAULT_BACKFILL_LOOKBACK_MINUTES = 7 * 24 * 60


def grok_backfill_enabled() -> bool:
    return os.environ.get("GROK_BACKFILL", "0").strip() == "1"


def grok_backfill_lookback_minutes() -> int:
    raw = os.environ.get("GROK_BACKFILL_LOOKBACK_MINUTES", "").strip()
    if not raw:
        return DEFAULT_BACKFILL_LOOKBACK_MINUTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_BACKFILL_LOOKBACK_MINUTES
    return value if value > 0 else DEFAULT_BACKFILL_LOOKBACK_MINUTES


def grok_backfill_until() -> datetime:
    """取り戻しの上限 (この時刻より後に受信したメールは扱わない)。未設定・不正なら例外。"""
    raw = os.environ.get("GROK_BACKFILL_UNTIL", "").strip()
    if not raw:
        raise ValueError("GROK_BACKFILL=1 には GROK_BACKFILL_UNTIL (ISO 8601) が必要です")
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as e:
        raise ValueError(f"GROK_BACKFILL_UNTIL が ISO 8601 ではありません: {raw!r}") from e
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


__all__ = [
    "DEFAULT_BACKFILL_LOOKBACK_MINUTES",
    "grok_backfill_enabled",
    "grok_backfill_lookback_minutes",
    "grok_backfill_until",
]
