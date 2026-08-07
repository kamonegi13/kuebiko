"""PIR Pydantic schema (Level 2 + Shadow fields).

互換性の最重要原則:
- migration 後の PIR は target_importance='auto' で挿入 → 既存 triage 判定を
  override しない。target_channel (R0 配信 override) は 2026-06-13 に撤去 —
  チャンネル決定権は routing に一本化 (PIR=関心の定義と評価 / routing=配信)。
- spotlight.enabled は default False。Spotlight ゼロから開始。
- Shadow fields (valid_from/valid_until/tags/weak_signals) と exclude_signals は
  2026-07-23 に撤去 — 観察期間 (~2ヶ月) で利用 0 件、かつ条件ツリー (match) が
  同じ意図をより正確に表現できる (弱補強 = keyword AND 枝、除外 = not 節)。
  過去データの残存キーは loader.strip_legacy_pir_keys が読み捨てる。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RoutingImportance = Literal["high", "medium", "low", "auto"]
SpotlightWindow = Literal["daily", "weekly", "monthly"]

# B2: 派生 signal 条件。routing_signals が算出する高精度 boolean に PIR を紐付ける。
# keyword/actor では表現できない CISA KEV カタログ照合・0day・japan-critical 等を
# PIR から条件化できるようにする (escalation 意図を PIR 化する B3 の前提)。
# routing 経路でのみ評価可 (DB 行には列が無いため KPI/preview では graceful skip)。
DerivedSignal = Literal[
    "kev",  # CISA KEV / active exploit (has_kev_or_active_exploit)
    "zero_day",  # 0day (is_zero_day)
    "japan_critical",  # 日本重要インフラ標的 (mentions_japan_critical)
    "japan_relevant",  # 日本関連 active threat (is_japan_security_relevant)
    "known_apt",  # 既知 APT 言及 (has_known_apt)
    "apt_leak",  # APT 内部リーク (is_apt_leak)
    "security_relevant",  # 広義セキュリティ関連 (is_security_relevant)
]


class StrongSignals(BaseModel):
    """強い signal (これにマッチしたら自動的に PIR match)。

    全 field 空 (= LLM 判定のみで match を決める) も許容。
    """

    model_config = ConfigDict(extra="forbid")

    keywords: list[str] = Field(default_factory=list)
    actors: list[str] = Field(default_factory=list)
    sectors: list[str] = Field(default_factory=list)
    # countries = **被害/標的国** (victim_country_iso 照合)。加害側の国籍はこちらでなく
    # actor_nations に書く (監査 backlog 2026-07-05: APT 系 PIR の意味反転修正)。
    countries: list[str] = Field(default_factory=list)
    # actor_nations = **アクター国籍** (ISO 3166-1 alpha-2)。記事の actor entity を
    # actor_aliases 辞書の nation で解決して照合する。「中国系 APT の動向」のように
    # 加害側の国家帰属で拾いたい PIR はこちら (中国=被害者の記事は match しない)。
    actor_nations: list[str] = Field(default_factory=list)
    feed_titles: list[str] = Field(default_factory=list)
    # B2: 派生 signal 条件 (OR 評価)。routing 時のみ供給され、KPI/preview (DB) では未評価。
    signals: list[DerivedSignal] = Field(default_factory=list)


class SpotlightConfig(BaseModel):
    """Spotlight 自動生成設定。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    title: str = ""
    window: SpotlightWindow = "weekly"


class LlmJudgeConfig(BaseModel):
    """概念 PIR の LLM 主題判定 (docs/pir_concept_llm_judge_design.md §4.4)。

    enabled=true のとき、``match`` (候補ゲート、決定論) を通過した記事にのみ
    focused judge が「主題として PIR に該当するか」を判定し、evaluator が
    match AND verdict を合成する。question 未設定なら title+description が
    判定基準になる (PIR is canonical intent)。
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    question: str = ""


class PirMetadata(BaseModel):
    """PIR の運用メタデータ (自動更新)。"""

    model_config = ConfigDict(extra="forbid")

    created_at: datetime | None = None
    updated_at: datetime | None = None
    migrated_from: str | None = None
    approved_by_user: bool = True
    rationale: str = ""


class Pir(BaseModel):
    """個別 PIR。

    description が canonical intent、structured fields はそこから compile されたもの。
    description 編集後は LLM compiler で再 compile することで全 field を再生成可能。
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    description: str = ""
    enabled: bool = True

    # Filter (Level 2 main)
    strong_signals: StrongSignals = Field(default_factory=StrongSignals)

    # signal-first 照合ツリー (docs/pir_signal_first_matching_design.md)。
    # routing と同一文法: all/any/not/always + leaf {property, op, value}。
    # None なら旧 strong_signals (生キーワード OR) で照合。PIR_SIGNAL_FIRST=1 かつ
    # match 非 None のとき signal-first 経路に切り替わる (evaluator.pir_match_signals)。
    match: dict[str, Any] | None = None

    # LLM 主題判定 (概念 PIR 限定)。match を候補ゲートとして AND 合成される。
    llm_judge: LlmJudgeConfig = Field(default_factory=LlmJudgeConfig)

    # 評価 (triage の importance 基準に注入。auto = 注入しない)
    target_importance: RoutingImportance = "auto"

    # Spotlight
    spotlight: SpotlightConfig = Field(default_factory=SpotlightConfig)

    # Metadata
    metadata: PirMetadata = Field(default_factory=PirMetadata)


class PirConfig(BaseModel):
    """config/pir.yaml の root schema。"""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    priorities: list[Pir] = Field(default_factory=list)

    def find(self, pir_id: str) -> Pir | None:
        for p in self.priorities:
            if p.id == pir_id:
                return p
        return None

    def enabled_priorities(self) -> list[Pir]:
        return [p for p in self.priorities if p.enabled]
