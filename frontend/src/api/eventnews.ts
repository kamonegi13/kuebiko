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
  /** 発信者種別 (Grok/X の account_class)。空 = 未分類 / 非 X。 */
  account_class: string;
  published_at: string | null;
  summary: string;
  joined_at: string;
  contributed_new_facts: boolean;
}

export interface EventNewsFacet {
  key: string;
  /** label = 表示名、vocab = 値のラベル解決に使う語彙名 (backend が指定)。 */
  label: string;
  vocab: string;
  values: { value: string; articles: number }[];
}

/** 記事画面の「Diamond / 判定」と同じ行を出すための集計。 */
export interface EventNewsJudgement {
  intent?: EventNewsFacet | null;
  intent_confidence?: { value: string; articles: number }[];
  texts?: { label: string; items: { text: string; source_index: number }[] }[];
  stance?: EventNewsFacet | null;
  victim_sector?: EventNewsFacet | null;
  victim_country?: EventNewsFacet | null;
  channel?: EventNewsFacet | null;
  category?: EventNewsFacet | null;
  pmesii?: { axis: string; articles: number }[];
}

/** 原記事から抽出済みのメタデータ (決定論の集約。LLM を通らない)。 */
export interface EventNewsMetadata {
  entities: {
    type: string;
    values: { value: string; articles: number }[];
    omitted: number;
    cvss?: Record<string, { score: number; severity: string }>;
    affected?: Record<string, { vendors: string[]; products: string[] }>;
  }[];
  subject_actors: { id: string; label: string; articles: number }[];
  facets: EventNewsFacet[];
  judgement: EventNewsJudgement;
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
  /** 裏取りの内訳 = 媒体単位の tier 分布 (記事数ではない)。 */
  corroboration: { tier: string; media: number }[];
  metadata: EventNewsMetadata;
  note: string;
}

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return (await r.json()) as T;
}

/** 一覧の絞り込み。記事側の条件はニュース検索と同じ語彙 (同じ経路で解決される)。 */
export interface EventNewsQuery {
  limit?: number;
  offset?: number;
  importance?: string;
  search?: string;
  category?: string;
  channel?: string;
  actor?: string;
  cve?: string;
  malware?: string;
  intent?: string;
  pir?: string;
  affected_vendor?: string;
  entity_type?: string;
  entity_value?: string;
  since_hours?: number;
}

export function fetchEventNews(q: EventNewsQuery = {}) {
  const p = new URLSearchParams({ limit: String(q.limit ?? 50) });
  if (q.offset) p.set("offset", String(q.offset));
  if (q.since_hours) p.set("since_hours", String(q.since_hours));
  for (const k of [
    "importance", "search", "category", "channel", "actor", "cve",
    "malware", "intent", "pir", "affected_vendor", "entity_type", "entity_value",
  ] as const) {
    const v = q[k];
    if (v) p.set(k, String(v));
  }
  return get<{ items: EventNewsListItem[]; note: string; scan_capped?: boolean }>(
    `/api/v1/eventnews?${p}`,
  );
}

export function fetchEventNewsDetail(id: string) {
  return get<EventNewsDetail>(`/api/v1/eventnews/${encodeURIComponent(id)}`);
}

/** 事象単位の memo / bookmark。記事単位のメモとは粒度が違う (事象は記事の集合)。 */
export interface EventNote {
  item_id: string;
  bookmarked: boolean;
  note: string;
  tags: string[];
  judgment: string;
  updated_at?: string | null;
}

export function fetchEventNote(itemId: string) {
  return get<EventNote>(`/api/v1/event-notes/${encodeURIComponent(itemId)}`);
}

export async function saveEventNote(
  itemId: string,
  body: Pick<EventNote, "bookmarked" | "note" | "tags" | "judgment">,
): Promise<EventNote> {
  const r = await fetch(`/api/v1/event-notes/${encodeURIComponent(itemId)}`, {
    method: "PUT",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return (await r.json()) as EventNote;
}
