/** 事象ニュースの一覧行。裏取りは independent_sources で表す (member_count ではない)。 */
export interface EventNewsListItem {
  id: string;
  headline: string;
  preview: string;
  status: "new" | "updated" | "reinforced" | "dormant";
  change_kind: "add" | "correct" | null;
  importance: string;
  member_count: number;
  independent_sources: number;
  state_media_count: number;
  unclassified_sources: number;
  best_source_tier: string;
  first_reported_at: string;
  last_reported_at: string;
  current_version: number;
  has_news: boolean;
}

export interface EventNewsFact {
  text: string;
  source_index: number;
  paragraph: number;
}

export interface EventNewsMember {
  index: number;
  article_id: string;
  title: string;
  url: string;
  feed_title: string;
  source_tier: string;
  published_at: string | null;
  summary: string;
  joined_at: string;
  contributed_new_facts: boolean;
}

export interface EventNewsDetail {
  id: string;
  status: string;
  change_kind: string | null;
  importance: string;
  independent_sources: number;
  state_media_count: number;
  unclassified_sources: number;
  best_source_tier: string;
  first_reported_at: string;
  last_reported_at: string;
  news: {
    version: number;
    generated_at: string;
    model: string;
    headline: string;
    bluf: string;
    facts: EventNewsFact[];
    discrepancies: EventNewsFact[];
    unknowns: string[];
    dropped_lines: number;
    resolved_ids: number;
    history: { version: number; generated_at: string }[];
  } | null;
  members: EventNewsMember[];
  note: string;
}

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return (await r.json()) as T;
}

export function fetchEventNews(limit = 50, importance?: string) {
  const q = new URLSearchParams({ limit: String(limit) });
  if (importance) q.set("importance", importance);
  return get<{ items: EventNewsListItem[]; note: string }>(`/api/v1/eventnews?${q}`);
}

export function fetchEventNewsDetail(id: string) {
  return get<EventNewsDetail>(`/api/v1/eventnews/${encodeURIComponent(id)}`);
}
