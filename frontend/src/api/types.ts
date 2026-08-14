// Backend (Python) との型同期。openapi-typescript 自動生成は将来の improvement。

import type { SourceBasis } from "./spotlight";

// アクター・ミッション脅威評価 (docs/actor_mission_threat_design.md)。
// tier=null は「未評価 (データ不足)」— Watch (評価済み低位) と区別する。
// 90d 固定窓で server 計算 (UI の表示窓と独立、辞書には保存しない)。
export interface ActorThreat {
  tier: "critical" | "high" | "moderate" | "watch" | null;
  tier_rule: string;
  relevance_band: number; // 0-3
  capability_band: "high" | "medium" | "low" | "unknown";
  activity_state: "spiking" | "active" | "quiet" | "dormant";
  relevance_factors: string[];
  capability_factors: string[];
  activity_factors: string[];
  coverage_note: string | null;
}

export interface ActorBrief {
  actor_id: string;
  canonical: string;
  aliases: string[];
  nation: string;
  family: string;
  mitre_group: string;
  // 実体種別 (Actors Stage 5): group=攻撃グループ / organization=国家機関 / contractor=請負
  kind: "group" | "organization" | "contractor";
  sponsor_org: string | null;
  total_articles: number;
  daily_counts: number[];
  sparkline: string;
  is_new: boolean;
  is_spike: boolean;
  spike_ratio: number | null;
  is_quiet_waking: boolean;
  japan_targeted_count: number;
  last_seen_iso: string;
  // Phase Diamond-Axes: socio-political 軸 = この actor の意図分布 [(intent, count), ...]
  top_intents: [string, number][];
  // ③ PIR 連携: この actor が該当する enabled PIR の id 群 (UI バッジ/優先用)
  matched_pir_ids: string[];
  // ミッション脅威評価 (/threats 系 endpoint のみ添付。undefined = 添付なし、
  // null = 評価対象外 (organization/contractor) or 評価障害)
  threat?: ActorThreat | null;
}

export interface ActorActivity extends ActorBrief {
  sponsor: string;
  description: string;
  top_sectors: [string, number][];
  top_countries: [string, number][];
  top_cves: string[];
  top_ttps: string[];
  // Phase Diamond: Capability + Infrastructure 軸
  top_malware_families: [string, number][];
  top_tools: [string, number][];
  top_iocs_ip: [string, number][];
  top_iocs_domain: [string, number][];
  top_iocs_hash: [string, number][];
  top_iocs_url: [string, number][];
}

export interface ActorArticle {
  article_id: string;
  title: string;
  url: string;
  feed_title: string;
  importance: string;
  created_at: string;
  posted_channel: string;
  // Phase Diamond-Axes: incident 単位の 2 軸
  socio_political_intent: string | null;
  intent_confidence?: string | null;
  technical_axis_summary: string | null;
}

export interface ActorRelations {
  family_members: [string, number][];
  cooccur_actors: [string, number][];
  related_campaigns: string[];
}

export interface ActorDetail {
  actor_id: string;
  found: boolean;
  activity?: ActorActivity;
  relations?: ActorRelations;
  recent_articles?: ActorArticle[];
  timeline_daily?: [string, number][];
  // 時間軸トグル: 発生時刻 (event_date) 系列 + カバレッジ。報道時刻 timeline_daily と切替え、
  // アクターの実際の活動年代 (作戦史) を dated subset で復元 (過去事象に偏る・1y+ で効く)。
  timeline_event?: [string, number][];
  timeline_coverage?: { dated: number; total: number; event_in_window: number };
  // 既知 TTP (Actor 辞書由来の knowledge、"T1566 Phishing" 形式)。
  // 観測 top_ttps との突き合わせ (既知外ハイライト) に使う
  known_ttps?: string[];
  // 配下グループ rollup (organization のみ): [actor_id, canonical, count, sparkline]
  child_groups?: [string, string, number, string][];
}

export interface DiscoveryPanel {
  new_actors: ActorBrief[];
  spiking_actors: ActorBrief[];
  waking_actors: ActorBrief[];
  unknown_bucket_count: number;
}

export interface FamilyInfo {
  nation: string;
  label: string;
  description: string;
}

export interface SnapshotResponse {
  lookback_days: number;
  families: Record<string, FamilyInfo>;
  nations: string[];
  filters: Record<string, string>;
  actor_lookup: Record<string, string>;
  discovery: DiscoveryPanel;
  actors_count: number;
}

export interface ThreatsResponse {
  actors: ActorBrief[];
  discovery: DiscoveryPanel;
}

export interface PMESIIIncident {
  title: string;
  url: string;
  feed_title: string;
  importance: string | null;
  created_at: string;
}

export interface PMESIICard {
  axis_id: string;
  display: string;
  // 供給劣化 (baseline 比 <20% への崩落 = 「事象なし」でなく計測の欠損)。旧レコード互換で optional
  is_degraded?: boolean;
  total_current: number;
  total_baseline_avg: string;
  spike_ratio_label: string;
  is_spike: boolean;
  is_new: boolean;
  top_sectors: { label: string; count: number }[];
  top_countries: { label: string; count: number }[];
  recent_incidents: PMESIIIncident[];
  related_actors: number;
}

export interface PMESIISynthesisContext {
  kind: "daily" | "weekly";
  headline: string;
  weight_section: string;
  period_start: string;
  period_end: string;
  generated_at: string;
  article_count: number;
}

export interface PMESIIResponse {
  cards: PMESIICard[];
  lookback_hours: number;
  baseline_weeks: number;
  non_empty_count: number;
  focused_axis: string;
  synthesis: PMESIISynthesisContext | null;
}

// 国家中心 情勢ボード (National Situation Board): サイバー↔地政学を国家で相関。
export interface SituationRecent {
  article_id: string;
  title: string;
  url: string;
  importance: string | null;
  intent: string | null;
  date: string | null;
}

export interface SituationFace {
  total: number;
  // cyber 攻撃者面=その国の APT / 標的面=攻撃元アクター
  actors?: { actor_id: string; label: string; count: number }[];
  sectors?: { sector: string; label: string; count: number }[]; // 標的面のみ (狙われた分野)
  intents: { intent: string; count: number }[];
  domains: { axis: string; label: string; count: number }[]; // PMESII 足場 (内訳)
  recent: SituationRecent[];
}

export interface SituationResponse {
  nation: string;
  label: string;
  window_days: number | null;
  cyber: SituationFace | null; // 攻撃者レンズ: その国の APT 攻勢 (actor.nation、帰属済み)
  cyber_mention: SituationFace | null; // 言及レンズ: 帰属なしサイバー言及 (i_cyber×当事国×actor無し、政策/態勢中心)
  cyber_target: SituationFace | null; // 標的レンズ: その国が受けている脅威 (victim_country)
  geopolitical: SituationFace | null;
  // 時間軸トグル: 報道時刻系列 (cyber/geopol) + 発生時刻系列 (cyber_event/geopol_event)
  // + カバレッジ (発生日付き dated / total、うち窓内 event_in_window)。
  tempo: {
    buckets: string[];
    cyber: number[];
    geopol: number[];
    cyber_event: number[];
    geopol_event: number[];
    coverage: { dated: number; total: number; event_in_window: number };
  };
}

export interface NationOption {
  iso: string;
  label: string;
  role: "home" | "adversary" | "allied" | "other"; // セレクタの役割グループ分け (home=自国)
  cyber: number;
  cyber_target: number;
  geopol: number;
  total: number;
}

export interface NationsResponse {
  nations: NationOption[];
}

export interface SynthesisLatest {
  headline: string;
  weight_section: string;
  chain_section: string;
  cog_section: string;
  spillover_section: string;
  pir_section: string;
  period_start: string;
  period_end: string;
  generated_at: string;
  article_count: number;
  llm_model: string;
}

export interface SynthesisEvent {
  label: string;
  summary: string;
  article_ids?: string[];
  source_basis?: SourceBasis; // S1: 出典基盤の確度
}

// S2: 分析トレードクラフト (ICD 203) — トンネル視を防ぐメタ分析
export interface Tradecraft {
  leading_assessment?: string;
  alternatives?: string[];
  key_assumptions?: string[];
  indicators?: string[];
  // 構造的矯正: 注入信号への明示応答 (出典割引 / FC2予測整合 / 鮮度判定)
  source_caveat?: string;
  forecast_alignment?: string;
  freshness_note?: string;
  // B(2): 予測 + 前期予測の採点 (的中率 calibration)
  forecasts?: { claim: string; horizon: string; confidence: string }[];
  forecast_scorecard?: { claim: string; verdict: string; reason: string }[];
  // 証拠駆動 synthesis (SYNTHESIS_GROUNDED) の canonical estimate (ACH/証拠/確度のトレーサビリティ)
  grounded_estimate?: GroundedEstimate;
}

export interface GroundedEvidence {
  article_id: string;
  source_tier: string;
  attribution_basis: string;
  excerpt: string;
  polarity: string; // supports / contradicts / neutral (leading 仮説基準)
}

export interface GroundedHypothesisScore {
  hypothesis: string;
  consistent: number;
  inconsistent: number;
  verdict: string; // leading / viable / refuted / unscored
}

export interface GroundedJudgment {
  id: string;
  claim: string;
  domain: string;
  leading_hypothesis: string;
  confidence: string; // high / moderate / low
  confidence_basis: string;
  hypotheses: GroundedHypothesisScore[];
  evidence: GroundedEvidence[];
  key_assumptions: string[];
  missing_evidence: string[];
  indicators: string[];
  adversarial_refuted: boolean;
  adversarial_note: string;
  // 割当のみで ACH 未評価の記事数 (状態分離 2026-07-16)。旧レコードには無い。
  unassessed_count?: number;
  // 台帳現在値 overlay (2026-07-25): 射影は生成時点の凍結スナップショットのため、
  // API が最新 revision の現在値を併記する。differs=true なら台帳が是正済み。
  ledger_now?: {
    rev: number;
    leading_hypothesis: string;
    confidence: string;
    updated_at: string;
    differs: boolean;
  };
}

export interface GroundedEstimate {
  period_type: string;
  judgments: GroundedJudgment[];
  model: string;
  considered_count?: number; // ノミネート時に考慮した記事プール総数 (接地数とは別)
}

export interface ForecastAccuracy {
  realized: number;
  partial: number;
  missed: number;
  /** 情勢が再評価されず指標を一度も照会できなかった件数。scored (分母) には含まれない。 */
  unevaluated: number;
  scored: number;
  hit_rate_pct: number | null;
}

export interface SynthesisResponse {
  period_type: "daily" | "weekly" | "monthly";
  has_data: boolean;
  latest?: SynthesisLatest;
  axes_evidence?: Record<string, SynthesisEvent[]>;
  tradecraft?: Tradecraft;
  forecast_accuracy?: ForecastAccuracy; // B(2): 予測の的中率
}

// Phase 4 将来予測 (Forecast)。backend src/forecast/models.py と同期。
export interface EntityTrend {
  scope: string;
  value: string;
  label: string;
  weekly: number[];
  total: number;
  direction: "increasing" | "stable" | "decreasing";
  slope: number;
  z_score: number;
  is_spike: boolean;
  // ④ PIR 連携: この trend (actor) が該当する enabled PIR の id 群
  matched_pir_ids: string[];
}

export interface CorrelationPair {
  scope: string;
  a: string;
  b: string;
  correlation: number;
}

export interface IndicatorHitStats {
  verified: number;
  hits: number;
  hit_rate: number;
}

// FC2 v2: 較正済み判定 (Poisson 2σ・週次正規化・hit/partial/miss)。v1 は参考表示に格下げ
export interface IndicatorVerdictStatsV2 {
  verified: number;
  hits: number;
  partials: number;
  misses: number;
  hit_rate: number;
}

export interface ForecastIndicator {
  id: number | null;
  period_type: string;
  period_start: string;
  scope: string;
  target_value: string;
  direction: string;
  z_score: number;
  baseline_avg: number;
  latest_count: number;
  rationale: string;
  created_at: string;
  verified_at: string | null;
  hit: boolean | null;
  observed_count: number;
}

export interface ForecastResponse {
  generated_at: string;
  weeks: number;
  has_data: boolean;
  spike_alerts: EntityTrend[];
  actor_trends: EntityTrend[];
  intent_trends: EntityTrend[];
  correlations: CorrelationPair[];
  indicator_stats: IndicatorHitStats;
  indicator_stats_v2?: IndicatorVerdictStatsV2;
  open_indicators: ForecastIndicator[];
}

// Phase 6 過去参照 (Retrospect / time-machine)。
export interface RetrospectSynthesis {
  headline: string;
  weight_section: string;
  chain_section: string;
  cog_section: string;
  spillover_section: string;
  pir_section: string;
  article_count: number;
  generated_at: string;
}

export interface RetrospectArticle {
  article_id: string;
  title: string;
  url: string;
  importance: string | null;
  category: string | null;
  socio_political_intent: string | null;
  intent_confidence?: string | null;
  created_at: string | null;
}

export interface RetrospectForecastOutcome {
  scope: string;
  target_value: string;
  direction: string;
  z_score: number;
  verified: boolean;
  hit: boolean | null;
  observed_count: number;
}

// その週の週次深掘りダイジェスト本文 (weekly_recaps 永続化、job id: weekly-recap)
export interface RetrospectRecap {
  period_label: string;
  recap_text: string;
  candidate_count: number;
  generated_at: string;
}

export interface RetrospectResponse {
  weeks_ago: number;
  period: { start: string; end: string; label: string };
  synthesis: RetrospectSynthesis | null;
  recap: RetrospectRecap | null;
  top_articles: RetrospectArticle[];
  top_actors: { value: string; count: number }[];
  forecast_outcomes: RetrospectForecastOutcome[];
  has_data: boolean;
}

// ブリーフ閲覧時の補足コンテキスト (時間軸統合 P2/P3): 閲覧時計算、配信物には焼き込まない
export interface BriefContextResponse {
  window_label: string;
  top_actors: { value: string; count: number }[];
  forecast_indicators: {
    scope: string;
    target_value: string;
    direction: string;
    verified: boolean;
    hit: boolean | null;
  }[];
}

export interface WsEvent {
  type: string;
  payload: Record<string, unknown>;
}

// W1 (通知再設計): 日次ブリーフ (朝刊/夕刊) の Web 通読。
export interface DailyBriefSource {
  title: string;
  url: string;
}

export interface BriefSynthesisSection {
  key: string;
  label: string;
  text: string;
}

export interface BriefTradecraft {
  leading_assessment?: string;
  alternatives?: string[];
  key_assumptions?: string[];
  indicators?: string[];
}

export interface BriefSynthesisPayload {
  headline: string;
  sections: BriefSynthesisSection[];
  tradecraft: BriefTradecraft | null;
}

export interface BriefPirMatch {
  title: string;
  url: string;
  importance: string; // high / medium / low
  feed: string;
  tier: string; // official / social / その他
}

export interface BriefPirSection {
  title: string;
  total: number;
  summary: string;
  matches: BriefPirMatch[];
}

// 構造化 payload (2026-07-12): Web はテキストでなく構造から描画する。旧行は null。
export interface DailyBriefPayload {
  synthesis: BriefSynthesisPayload | null;
  pir: BriefPirSection[];
}

export interface DailyBrief {
  id: number;
  slot: string; // 'morning' (朝刊) / 'evening' (夕刊)
  period_label: string;
  title: string;
  bluf: string;
  summary: string;
  section_count: number;
  sources: DailyBriefSource[];
  payload: DailyBriefPayload | null;
  generated_at: string;
}

/** 一覧サイドバー用の軽量メタ (本文なし)。本文は /daily-briefs/{id} で選択時に取得 */
export type DailyBriefMeta = Pick<
  DailyBrief,
  "id" | "slot" | "period_label" | "title" | "section_count" | "generated_at"
>;

export interface DailyBriefMetasResponse {
  briefs: DailyBriefMeta[];
}

export interface DailyBriefDetailResponse {
  brief: DailyBrief;
}

export interface DailyBriefsResponse {
  briefs: DailyBrief[];
}

// 分析チャット (2026-07-12): 自然言語 → データツール実行 → 接地回答。
export interface AssistantTurn {
  role: "user" | "assistant";
  content: string;
}

export interface AssistantToolRun {
  tool: string;
  args: Record<string, unknown>;
  summary: string;
}

export interface AssistantChatResponse {
  answer: string;
  tools: AssistantToolRun[];
  model: string;
}
