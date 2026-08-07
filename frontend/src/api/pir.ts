// PIR (Priority Intelligence Requirements) API client。
// 詳細は src/ui/api/pir.py の各 endpoint 参照。

async function getJson<T>(path: string): Promise<T> {
  const r = await fetch(path, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`HTTP ${r.status}: ${path}`);
  return r.json() as Promise<T>;
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const text = await r.text();
    throw new Error(`HTTP ${r.status}: ${text}`);
  }
  return r.json() as Promise<T>;
}

async function deleteJson<T>(path: string): Promise<T> {
  const r = await fetch(path, { method: "DELETE", credentials: "same-origin" });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json() as Promise<T>;
}

// ===== Types =====

export type RoutingImportance = "high" | "medium" | "low" | "auto";
export type SpotlightWindow = "daily" | "weekly" | "monthly";
export type ConfidenceLevel = "high" | "medium" | "low";

// B2: 派生 signal 条件 (routing 時の高精度フラグ)。
export type DerivedSignal =
  | "kev"
  | "zero_day"
  | "japan_critical"
  | "japan_relevant"
  | "known_apt"
  | "apt_leak"
  | "security_relevant";

export interface StrongSignals {
  keywords: string[];
  actors: string[];
  sectors: string[];
  /** 被害/標的国 (victim_country 照合)。加害側の国籍は actor_nations へ */
  countries: string[];
  /** アクター国籍 (加害側、actor 辞書の nation 照合) — 2026-07-05 意味反転修正 */
  actor_nations: string[];
  feed_titles: string[];
  signals: DerivedSignal[];
}

/**
 * 照合条件ツリー (signal-first、authoring 統一 2026-07-23)。
 * combinator (all/any/not/always) または leaf {property, op, value}。
 * これが実際の記事照合を駆動する — strong_signals は補助 (脅威ページ連携・旧方式の予備)。
 */
export interface MatchNode {
  all?: MatchNode[];
  any?: MatchNode[];
  not?: MatchNode;
  always?: boolean;
  property?: string;
  op?: string;
  value?: string | string[];
}

/** 概念 PIR の LLM 主題判定 (候補ゲート = match を通過した記事にのみ夜間バッチが判定)。 */
export interface LlmJudgeConfig {
  enabled: boolean;
  question: string;
}

export interface SpotlightConfig {
  enabled: boolean;
  title: string;
  window: SpotlightWindow;
}

export interface PirMetadata {
  created_at: string | null;
  updated_at: string | null;
  migrated_from: string | null;
  approved_by_user: boolean;
  rationale: string;
}

export interface Pir {
  id: string;
  title: string;
  description: string;
  enabled: boolean;
  strong_signals: StrongSignals;
  /** 照合条件 (null = 旧方式: strong_signals keyword 照合)。編集往復で必ず保全すること。 */
  match: MatchNode | null;
  llm_judge: LlmJudgeConfig;
  target_importance: RoutingImportance;
  spotlight: SpotlightConfig;
  metadata: PirMetadata;
}

export interface PirListItem {
  id: string;
  title: string;
  description: string;
  enabled: boolean;
  target_importance: string;
  spotlight_enabled: boolean;
  match_count_7d: number;
  match_count_30d: number;
  last_match_at: string | null;
  approved_by_user: boolean;
}

export interface PirListResponse {
  version: number;
  priorities: PirListItem[];
  pending_draft_count: number;
}

export interface CompileResponse {
  pir: Pir;
  confidence: Record<string, ConfidenceLevel>;
  rationale: string;
  warnings: string[];
}

export interface PreviewMatchSample {
  article_id: string;
  title: string;
  url: string;
  feed_title: string;
  importance: string;
  posted_channel: string | null;
  created_at: string;
  matched_via: string[];
}

export interface PreviewResponse {
  match_count: number;
  samples: PreviewMatchSample[];
}

export interface KpiResponse {
  pir_id: string;
  match_count_7d: number;
  match_count_30d: number;
  last_match_at: string | null;
  top_feeds: [string, number][];
  top_actors: [string, number][];
  channel_distribution: Record<string, number>;
  samples: PreviewMatchSample[];
}

export interface PirHeadline {
  title: string;
  url: string;
  importance: string;
  feed_title: string;
  created_at: string;
}

export interface DashboardOverviewItem {
  pir_id: string;
  title: string;
  enabled: boolean;
  match_count_7d: number;
  match_count_30d: number;
  last_match_at: string | null;
  sparkline_7d: number[];
  headlines?: PirHeadline[];
}

export interface DashboardOverviewResponse {
  items: DashboardOverviewItem[];
}

// ===== Helpers =====

export function newEmptyPir(id = ""): Pir {
  return {
    id,
    title: "",
    description: "",
    enabled: true,
    strong_signals: {
      keywords: [],
      actors: [],
      sectors: [],
      countries: [],
      actor_nations: [],
      feed_titles: [],
      signals: [],
    },
    match: null,
    llm_judge: { enabled: false, question: "" },
    target_importance: "auto",
    spotlight: { enabled: false, title: "", window: "weekly" },
    metadata: {
      created_at: null,
      updated_at: null,
      migrated_from: null,
      approved_by_user: false,
      rationale: "",
    },
  };
}

// ===== API =====

export const pirApi = {
  list: () => getJson<PirListResponse>("/api/v1/pir"),
  get: (id: string) => getJson<Pir>(`/api/v1/pir/${encodeURIComponent(id)}`),
  kpi: (id: string) => getJson<KpiResponse>(`/api/v1/pir/${encodeURIComponent(id)}/kpi`),
  compile: (title: string, description: string, pir_id?: string) =>
    postJson<CompileResponse>("/api/v1/pir/compile", { title, description, pir_id }),
  preview: (pir: Pir, lookback_hours = 168) =>
    postJson<PreviewResponse>("/api/v1/pir/preview", { pir, lookback_hours }),
  save: (pir: Pir, is_new: boolean) =>
    postJson<Pir>("/api/v1/pir/save", { pir, is_new }),
  approve: (id: string) => postJson<Pir>(`/api/v1/pir/${encodeURIComponent(id)}/approve`, {}),
  toggle: (id: string) => postJson<Pir>(`/api/v1/pir/${encodeURIComponent(id)}/toggle`, {}),
  delete: (id: string) => deleteJson<{ deleted: boolean }>(`/api/v1/pir/${encodeURIComponent(id)}`),
  dashboardOverview: () => getJson<DashboardOverviewResponse>("/api/v1/pir/dashboard/overview"),
};
