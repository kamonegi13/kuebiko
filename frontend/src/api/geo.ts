// 脅威マップ (Geo-Cyber Map) の API クライアント。
// backend: /api/v1/geo/cyber-map (read-only、counts のみ)。

export interface GeoSectorBreakdown {
  sector: string;
  label: string;
  count: number;
}

// カバレッジ信頼度 (corpus 由来): 薄い/単一 = ソース≤2 or 単一依存≥70%、厚い = ソース≥5。
export type GeoConfidence = "thin" | "medium" | "rich";

export interface GeoNode {
  iso: string;
  label: string;
  lat: number;
  lon: number;
  count: number;
  precision: string; // "country" (Phase 3 は国粒度のみ)
  top_sector: string | null;
  sectors: GeoSectorBreakdown[];
  // 動機内訳 (色分けトグル「動機」で cyber バブルをパイ塗り)。GeoIntentBreakdown は下方で定義。
  top_intent: string | null;
  intents: GeoIntentBreakdown[];
  intent_count: number;
  // カバレッジ信頼層: この国の像が何ソースに支えられているか (収集網の偏りを正直に符号化)。
  source_count: number;
  top_source_share: number; // 0–1: 最大単一ソースのシェア (高いほど monoculture)
  top_source: string | null; // 支配ソースの表示名 (例: ransomware.live)
  posted_count: number; // 報道 (triage 済) 件数
  collected_count: number; // 台帳 (生レジストリ) 件数
  confidence: GeoConfidence;
}

export interface GeoActorVictim {
  iso: string;
  label: string;
  lat: number;
  lon: number;
  count: number;
}

export interface GeoActor {
  actor_id: string;
  label: string;
  nation: string | null;
  nation_label: string | null;
  nation_lat: number | null;
  nation_lon: number | null;
  total: number;
  victims: GeoActorVictim[];
}

export interface GeoIntentBreakdown {
  intent: string;
  count: number;
}

// 地政学イベント (geopolitical/policy) の国マーカ (#6 別レイヤー)。
// 統一 intent 軸で **動機色分け**: top_intent = 国の支配動機 (色)、intents = 内訳。
export interface GeoEventNode {
  iso: string;
  label: string;
  lat: number;
  lon: number;
  count: number;
  top_intent: string | null; // 支配動機 (色分け用、未判定多数なら null)
  intents: GeoIntentBreakdown[]; // 動機内訳 (多い順)
  intent_count: number; // 動機が判定できた件数 (count のうち)
}

export interface CyberMapResponse {
  window_days: number | null;
  generated_at: string;
  nodes: GeoNode[];
  actors: GeoActor[];
  geo_events: GeoEventNode[];
  unplaced_count: number;
  // 地図に乗らなかった件数の内訳 (誠実性): 非サイバー(言及/地政学) / 運用ミス / 国不明。
  excluded: { non_cyber: number; accidental: number; unplaced: number };
  totals: { placed_events: number; countries: number };
  // 時間軸トグル: 報道時刻(report) / 発生時刻(event)。event は dated subset のみ表示。
  time_basis?: "report" | "event";
  time_coverage?: { dated: number; total: number };
}

// 時間軸: 報道/取得時刻 (collection が出している) / 事象の実発生時刻 (dated のみ)。
export type TimeBasis = "report" | "event";

// 脅威種別フィルタ: 全て / ランサム (ransomware.live + ransom_group) / その他。
export type ThreatClass = "all" | "ransomware" | "other";
// 表示ドメイン: サイバー攻撃被害 / 地政学イベント (地図レイヤーと連動)。
export type GeoDomain = "cyber" | "geopolitical";
// 収集経路: すべて / 報道(posted=triage済ニュース) / 台帳(collected=ransomware.live 等の生レジストリ)。
export type SourceStatus = "all" | "posted" | "collected";
// 意義の地図: 全件 / 中+ (high+medium) / 高のみ。importance は PIR 駆動 triage の holistic な significance。
export type MinImportance = "all" | "medium_up" | "high";
// PMESII 直交フィルタ (地政学レイヤー): 全領域 / 政治 / 軍事 / 経済 / 社会 / 情報インフラ /
// サイバー / 環境 / 技術。色=動機(intent) と直交する「どの領域に作用するか」の絞り込み。
export type PmesiiAxis = "all" | "p" | "m" | "e" | "s" | "i_infra" | "i_cyber" | "p_env" | "t";

export async function fetchCyberMap(
  days: number,
  threatClass: ThreatClass = "all",
  sourceStatus: SourceStatus = "all",
  minImportance: MinImportance = "all",
  pmesii: PmesiiAxis = "all",
  timeBasis: TimeBasis = "report",
): Promise<CyberMapResponse> {
  const u = new URLSearchParams({
    days: String(days),
    threat_class: threatClass,
    source_status: sourceStatus,
    min_importance: minImportance,
    pmesii,
    time_basis: timeBasis,
  });
  const r = await fetch(`/api/v1/geo/cyber-map?${u.toString()}`, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`cyber-map fetch failed: ${r.status}`);
  return (await r.json()) as CyberMapResponse;
}

// 地図下の日次件数推移 (国別/セクター別 上位系列 + その他)。
export interface TrendSeries {
  key: string;
  label: string;
  counts: number[];
}

export interface TrendResponse {
  group_by: "country" | "sector";
  buckets: string[]; // YYYY-MM-DD の連続日
  series: TrendSeries[];
}

export async function fetchTrend(
  days: number,
  threatClass: ThreatClass,
  groupBy: "country" | "sector",
  domain: GeoDomain = "cyber",
  sourceStatus: SourceStatus = "all",
  isos?: string[], // 地域フィルタ: 対象国 ISO に推移を絞る (空/未指定=全体)。
  minImportance: MinImportance = "all",
  pmesii: PmesiiAxis = "all",
): Promise<TrendResponse> {
  const u = new URLSearchParams({
    days: String(days),
    threat_class: threatClass,
    group_by: groupBy,
    domain,
    source_status: sourceStatus,
    min_importance: minImportance,
    pmesii,
  });
  if (isos && isos.length > 0) u.set("isos", isos.join(","));
  const r = await fetch(`/api/v1/geo/trend?${u.toString()}`, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`trend fetch failed: ${r.status}`);
  return (await r.json()) as TrendResponse;
}

export interface CountryArticle {
  article_id: string;
  title: string | null;
  url: string | null;
  importance: string | null;
  category: string | null;
  victim_sector: string | null;
  victim_sector_label: string | null;
  socio_political_intent: string | null; // 動機 (統一 intent 軸、geopolitical で色チップ表示)
  intent_confidence?: string | null; // null=旧レジーム確定 / low=仮説
  status: string | null; // posted=報道 / collected=台帳 (生レジストリ)
  created_at: string | null;
}

export interface CountryArticlesResponse {
  iso: string;
  label: string;
  count: number;
  articles: CountryArticle[];
}

export async function fetchCountryArticles(
  iso: string,
  days: number,
  threatClass: ThreatClass = "all",
  domain: GeoDomain = "cyber",
  sourceStatus: SourceStatus = "all",
  minImportance: MinImportance = "all",
  pmesii: PmesiiAxis = "all",
): Promise<CountryArticlesResponse> {
  const u = new URLSearchParams({
    days: String(days),
    threat_class: threatClass,
    domain,
    source_status: sourceStatus,
    min_importance: minImportance,
    pmesii,
  });
  const r = await fetch(`/api/v1/geo/country/${encodeURIComponent(iso)}?${u.toString()}`, {
    credentials: "same-origin",
  });
  if (!r.ok) throw new Error(`country articles fetch failed: ${r.status}`);
  return (await r.json()) as CountryArticlesResponse;
}
