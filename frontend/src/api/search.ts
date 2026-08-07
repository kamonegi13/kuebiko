// 統合検索 API (hybrid retrieval + LLM rerank/planning)。backend: src/search/
// facet (importance/category/feed/channel/cve/malware/intent/期間) を渡すと
// 検索結果に hard filter を AND 合成する (browse facet と同一語彙)。

export interface SearchHitItem {
  article_id: string;
  title: string;
  url: string;
  importance: string | null;
  category: string | null;
  feed_title: string | null;
  socio_political_intent: string | null;
  intent_confidence?: string | null;
  summary: string | null;
  created_at: string | null;
  score: number;
  rerank_score: number | null;
  reason: string;
  matched_via: string[];
}

export interface SearchResponse {
  query: string;
  mode: string;
  count: number;
  results: SearchHitItem[];
  reranked: boolean;
  embeddings_available: boolean;
}

// browse と search で共有する facet 群 (空文字/undefined は無効)。
export interface SearchFacets {
  importance?: string;
  category?: string;
  feed?: string;
  channel?: string;
  cve?: string;
  malware?: string;
  intent?: string;
  pir?: string;
  actor?: string;
  affected_vendor?: string;
  body?: string; // "stump"(切り株) / "full"(全文取得済)
  since_hours?: number;
}

export async function fetchSearch(
  query: string,
  mode: "quick" | "precise",
  facets: SearchFacets = {},
  limit = 25,
): Promise<SearchResponse> {
  const qs = new URLSearchParams({ query, mode, limit: String(limit) });
  if (facets.importance) qs.set("importance", facets.importance);
  if (facets.category) qs.set("category", facets.category);
  if (facets.feed) qs.set("feed", facets.feed);
  if (facets.channel) qs.set("channel", facets.channel);
  if (facets.cve) qs.set("cve", facets.cve);
  if (facets.malware) qs.set("malware", facets.malware);
  if (facets.intent) qs.set("intent", facets.intent);
  if (facets.pir) qs.set("pir", facets.pir);
  if (facets.actor) qs.set("actor", facets.actor);
  if (facets.affected_vendor) qs.set("affected_vendor", facets.affected_vendor);
  if (facets.since_hours) qs.set("since_hours", String(facets.since_hours));
  const r = await fetch(`/api/v1/search?${qs.toString()}`, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`search failed: ${r.status}`);
  return (await r.json()) as SearchResponse;
}

// affected facet の autocomplete 候補 (NVD cache 由来の vendor + product)。
export async function fetchAffectedVendors(): Promise<string[]> {
  const r = await fetch(`/api/v1/affected-vendors`, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`affected-vendors failed: ${r.status}`);
  const j = (await r.json()) as { vendors: string[] };
  return j.vendors ?? [];
}

// actor facet の選択肢 (canonical id → 表示名)。
export interface ActorOption {
  id: string;
  name: string;
}
export async function fetchActorOptions(): Promise<ActorOption[]> {
  const r = await fetch(`/api/v1/actor-options`, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`actor-options failed: ${r.status}`);
  const j = (await r.json()) as { actors: ActorOption[] };
  return j.actors ?? [];
}
