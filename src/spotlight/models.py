"""Spotlight record schema (LLM 出力 + DB row 兼用)。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SpotlightPeriod = Literal["daily", "weekly", "monthly"]


class KeyEvent(BaseModel):
    """Spotlight に含める key event 1 件。"""

    model_config = ConfigDict(extra="forbid")

    article_id: str
    title: str
    url: str = ""
    feed_title: str = ""
    importance: str = ""
    published_at: str = ""  # ISO 形式 (frontend で formatJst 経由表示)


class SpotlightRecord(BaseModel):
    """1 PIR × 1 period の Spotlight 結果。

    DB の pir_spotlight テーブル row と同一 schema (period_type と pir_id で
    一意、UPSERT 運用)。
    """

    model_config = ConfigDict(extra="forbid")

    pir_id: str
    pir_title: str  # 当時の PIR title (PIR yaml 変更で乖離する可能性、 snapshot)
    period_type: SpotlightPeriod
    period_start: datetime  # UTC
    period_end: datetime  # UTC
    headline: str  # 1-2 文の状況総括 (60-120 字目安)
    outlook: str  # 短い展望 / 来週見るべき指標 (200-400 字目安)
    key_events: list[KeyEvent] = Field(default_factory=list)
    article_count: int = 0  # 該当 PIR の match 総件数 (period 内)
    llm_model: str = ""  # 生成に使った LLM model 名 (26B vs 31B 比較用)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # Phase 2 K6: Discord (watch) 配信済時刻。None=未配信 (再配信 dedup の判定に使う)。
    posted_at: datetime | None = None
