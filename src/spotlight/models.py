"""Spotlight record schema (LLM 出力 + DB row 兼用)。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: rolling7 = **直近 7 日を毎日作り直す**窓 (2026-08-29)。
#:
#: ⭐ 日次の窓では材料が足りない。実測で 20 PIR 中 7 件は 1 日あたり 0〜2.6 件しか
#: 該当が無く (中国 APT 2.6 / 北朝鮮 1.9 / ロシア 1.1)、Spotlight の形式
#: (主要事象 5〜8 件 + 見通し 600〜1000 字) を求めると LLM が埋める。
#: 7 日窓なら 20 件中 17 件が 5 件以上を確保できる。
#: 週次との違いは鮮度 — 週次は最大 7 日古いが、rolling7 は毎日作り直す。
SpotlightPeriod = Literal["daily", "rolling7", "weekly", "monthly"]

#: Spotlight だけが持つ period。synthesis の期間にもジョブの周期にも存在しない。
#: 語彙との一致テストは、この集合を差し引いてから他の定数と突き合わせる。
SPOTLIGHT_ONLY_PERIODS: frozenset[str] = frozenset({"rolling7"})


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
    # schema 整合 (2026-09-07): 「不確実性を明示欄に書く」行動を event/synthesis と揃える。
    # 正直さドクトリン (過確信のみ防ぐ) の spotlight への延長でもあり、N 族 SFT の
    # 混合干渉 (SYNTHESIS §19: 欄を持たない spotlight が event の caveats を崩壊させた)
    # の根治でもある。旧行は NULL → 空 list で読む (後方互換)。
    caveats: list[str] = Field(default_factory=list)  # 留保・単一ソース注意等 (0-4 件)
    unknowns: list[str] = Field(default_factory=list)  # 現時点で分からないこと (0-4 件)
    article_count: int = 0  # 該当 PIR の match 総件数 (period 内)
    llm_model: str = ""  # 生成に使った LLM model 名 (26B vs 31B 比較用)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # Phase 2 K6: Discord (watch) 配信済時刻。None=未配信 (再配信 dedup の判定に使う)。
    posted_at: datetime | None = None
