// PIR Spotlight API client。

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

export type SpotlightPeriod = "daily" | "weekly" | "monthly";

export interface SourceBasis {
  confidence: "high" | "medium" | "low";
  best_tier: "official" | "research" | "news" | "social" | "state_media" | "unknown";
  source_count: number;
  has_official_authority: boolean;
  social_only: boolean;
  reason: string;
}

export interface KeyEvent {
  article_id: string;
  title: string;
  url: string;
  feed_title: string;
  importance: string;
  published_at: string;
  source_basis: SourceBasis;
}

export interface SpotlightSummary {
  pir_id: string;
  pir_title: string;
  period_type: string;
  period_start: string;
  period_end: string;
  headline: string;
  outlook: string;
  key_events: KeyEvent[];
  article_count: number;
  llm_model: string;
  generated_at: string;
}

export interface SpotlightListResponse {
  period_type: string;
  items: SpotlightSummary[];
}

export const spotlightApi = {
  list: (period_type: SpotlightPeriod = "weekly") =>
    getJson<SpotlightListResponse>(`/api/v1/spotlight?period_type=${period_type}`),
  get: (pir_id: string, period_type: SpotlightPeriod = "weekly") =>
    getJson<SpotlightSummary>(`/api/v1/spotlight/${encodeURIComponent(pir_id)}?period_type=${period_type}`),
  regenerate: (pir_id: string, period_type: SpotlightPeriod = "weekly", model?: string) =>
    postJson<SpotlightSummary>(`/api/v1/spotlight/${encodeURIComponent(pir_id)}/regenerate`, {
      period_type,
      model,
    }),
};
