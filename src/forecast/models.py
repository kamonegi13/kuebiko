"""Phase 4: forecasting のデータモデル。

- ``ForecastIndicatorRecord``: FC2 の監視指標 (spike から導き、翌週に的中検証する)。
- ``EntityTrend`` / ``CorrelationPair`` / ``ForecastResult``: read API の戻り構造。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# 指標の対象軸。article_entities の entity_type または articles.socio_political_intent。
# sector / country (I&W 日次バースト検出、burst.py) は victim_sector_canonical /
# victim_country_iso 由来。
ForecastScope = Literal["actor", "intent", "cve", "malware_family", "sector", "country"]


class ForecastIndicatorRecord(BaseModel):
    """FC2: ある期間に「来週警戒すべき」と判定した監視指標 1 件。

    spike 分析から決定的に導出し (period_start)、次期間に同 target の活動が実際に
    継続/上昇したかを検証 (verified_at / hit / observed_count) する accountability ループ。
    (period_type, period_start, scope, target_value) で一意。
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: int | None = None
    period_type: str  # 'weekly' / 'monthly'
    period_start: datetime
    scope: ForecastScope
    target_value: str  # actor id / intent / CVE / malware family
    direction: str  # 'rising' (spike) / 'watch'
    z_score: float = 0.0  # 予測時点の spike z-score
    baseline_avg: float = 0.0  # 予測時点の baseline 平均活動
    latest_count: int = 0  # 予測時点の直近週活動
    rationale: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # ----- 検証 (次期間に埋まる) -----
    verified_at: datetime | None = None
    hit: bool | None = None  # None=未検証、True=活動継続/上昇、False=沈静化
    observed_count: int = 0  # 検証窓での実活動件数


class EntityTrend(BaseModel):
    """1 entity (actor / intent 等) の週次トレンド + spike 判定 (FC3/FC4)。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    scope: ForecastScope
    value: str
    label: str = ""  # 表示名 (actor の display 名など、無ければ value)
    weekly: list[int] = Field(default_factory=list)  # 古い→新しい
    total: int = 0
    direction: str = "stable"
    slope: float = 0.0
    z_score: float = 0.0
    is_spike: bool = False
    # ④ PIR 連携: この trend (actor) が該当する enabled PIR の id 群。
    # user の PIR と無関係な global spike に埋もれず「自分が重視する脅威の予兆」を
    # 優先表示するための signal (intent scope は対象外で空)。
    matched_pir_ids: list[str] = Field(default_factory=list)


class CorrelationPair(BaseModel):
    """2 entity の活動相関 (FC5)。同時に動く actor 同士などを示す。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    scope: ForecastScope
    a: str
    b: str
    correlation: float


class IndicatorHitStats(BaseModel):
    """FC2: 直近の指標的中率サマリ (予測の説明責任)。

    v1 判定 (観測 >= baseline) は準トートロジーで ~97% に張り付き弁別力がない
    (監査 2026-07-16) — 履歴の意味を保つため残置し、主表示は v2 を使う。
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    verified: int = 0
    hits: int = 0
    hit_rate: float = 0.0


class IndicatorVerdictStatsV2(BaseModel):
    """FC2 v2: 較正済みの指標判定サマリ (Poisson 2σ・週次正規化・hit/partial/miss)。

    hit = 週次正規化した観測が baseline + 2√baseline 以上 (有意な増勢)。
    partial = baseline ± 2σ の帯 (基礎率での継続)。miss = 下振れ or 観測ゼロ。
    固定倍率と違い、偶然 hit 率が全指標で ~2-3% に揃う (設計
    docs/surfaces_remediation_design_2026_07_16.md §4b)。
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    verified: int = 0
    hits: int = 0
    partials: int = 0
    misses: int = 0
    hit_rate: float = 0.0


class ForecastResult(BaseModel):
    """read API ``/forecast`` の戻り。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    weeks: int = 8
    has_data: bool = False
    spike_alerts: list[EntityTrend] = Field(default_factory=list)  # FC3
    actor_trends: list[EntityTrend] = Field(default_factory=list)  # FC4
    intent_trends: list[EntityTrend] = Field(default_factory=list)  # FC4
    correlations: list[CorrelationPair] = Field(default_factory=list)  # FC5
    indicator_stats: IndicatorHitStats = Field(default_factory=IndicatorHitStats)  # FC2 (v1 参考)
    # FC2 v2 (主表示): 較正済み判定 (Poisson 2σ・週次正規化)
    indicator_stats_v2: IndicatorVerdictStatsV2 = Field(default_factory=IndicatorVerdictStatsV2)
    open_indicators: list[ForecastIndicatorRecord] = Field(default_factory=list)  # FC2 現行監視中
