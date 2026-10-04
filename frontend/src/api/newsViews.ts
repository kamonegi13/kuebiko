// ニュースの絞り込みビュー (利用者保存) の API クライアント。
// backend: src/ui/api/news_views.py。既定ビュー (BUILTIN_VIEWS) はここでは扱わない
// (frontend/src/components/news/views.ts が静的に持つ)。

import type { NewsFilters, NewsView } from "../components/news/views";

/** backend の NewsViewFilters (wire 形式。すべて snake_case・任意)。 */
interface WireFilters {
  search?: string;
  min_severity?: "" | "S3" | "S2" | "S1";
  relevant_only?: boolean;
  include_strategic?: boolean;
  jp?: "" | "targeted_affected" | "mentioned";
  since?: string;
  sort?: string;
  category?: string;
  feed?: string;
  intent?: string;
  pir?: string;
  actor?: string;
  affected_vendor?: string;
  body?: string;
  channel?: string;
  min_independent_sources?: number;
  has_news?: boolean;
  status?: string;
}

interface WireView {
  id: string;
  label: string;
  filters: WireFilters;
}

/** NewsFilters (部分) → wire 形式。空文字/false は送らない (filters={} が「絞り込み無し」)。 */
export function toWireFilters(f: Partial<NewsFilters>): WireFilters {
  const w: WireFilters = {};
  if (f.search) w.search = f.search;
  if (f.minSeverity) w.min_severity = f.minSeverity;
  if (f.relevantOnly) w.relevant_only = true;
  if (f.includeStrategic) w.include_strategic = true;
  if (f.jp) w.jp = f.jp;
  if (f.since && f.since !== "0") w.since = f.since;
  if (f.sort) w.sort = f.sort;
  if (f.category) w.category = f.category;
  if (f.feed) w.feed = f.feed;
  if (f.intent) w.intent = f.intent;
  if (f.pir) w.pir = f.pir;
  if (f.actor) w.actor = f.actor;
  if (f.affectedVendor) w.affected_vendor = f.affectedVendor;
  if (f.body) w.body = f.body;
  if (f.channel) w.channel = f.channel;
  if (f.minIndependentSources) w.min_independent_sources = 2;
  if (f.hasNews) w.has_news = true;
  if (f.newFactsOnly) w.status = "updated";
  return w;
}

/** wire 形式 → NewsFilters (部分、未指定フィールドは返さない)。 */
export function fromWireFilters(w: WireFilters): Partial<NewsFilters> {
  const f: Partial<NewsFilters> = {};
  if (w.search) f.search = w.search;
  if (w.min_severity) f.minSeverity = w.min_severity;
  if (w.relevant_only) f.relevantOnly = true;
  if (w.include_strategic) f.includeStrategic = true;
  if (w.jp) f.jp = w.jp;
  if (w.since) f.since = w.since;
  if (w.sort === "level") f.sort = "level";
  if (w.category) f.category = w.category;
  if (w.feed) f.feed = w.feed;
  if (w.intent) f.intent = w.intent;
  if (w.pir) f.pir = w.pir;
  if (w.actor) f.actor = w.actor;
  if (w.affected_vendor) f.affectedVendor = w.affected_vendor;
  if (w.body) f.body = w.body;
  if (w.channel) f.channel = w.channel;
  if ((w.min_independent_sources ?? 0) >= 2) f.minIndependentSources = true;
  if (w.has_news) f.hasNews = true;
  if (w.status === "updated") f.newFactsOnly = true;
  return f;
}

export function viewToWire(v: NewsView): WireView {
  return { id: v.id, label: v.label, filters: toWireFilters(v.filters) };
}

export function viewFromWire(w: WireView): NewsView {
  return { id: w.id, label: w.label, filters: fromWireFilters(w.filters) };
}

async function getJson<T>(path: string): Promise<T> {
  const r = await fetch(path, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`HTTP ${r.status}: ${path}`);
  return r.json() as Promise<T>;
}

export const newsViewsApi = {
  /** 利用者保存ビュー一覧 (ops の DB SSoT)。 */
  list: async (): Promise<NewsView[]> => {
    const r = await getJson<{ views: WireView[] }>("/api/v1/news-views");
    return r.views.map(viewFromWire);
  },
  /** 全量保存 (リネーム/削除も含め全件を送り直す)。 */
  save: async (views: NewsView[]): Promise<void> => {
    const body = { views: views.map(viewToWire) };
    const r = await fetch("/api/v1/news-views", {
      method: "PUT",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}: /api/v1/news-views`);
  },
};
