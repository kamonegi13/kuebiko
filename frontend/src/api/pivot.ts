// Phase 2 K2: 逆引き pivot API (entity → 参照記事 + 共起 entity)。

export interface PivotRelatedItem {
  value: string;
  count: number;
}

export interface PivotRelatedGroup {
  type: string;
  items: PivotRelatedItem[];
}

export interface PivotArticle {
  article_id: string;
  title: string;
  url: string;
  importance: string;
  category: string | null;
  posted_channel: string | null;
  socio_political_intent: string | null;
  intent_confidence?: string | null;
  created_at: string | null;
}

export interface PivotResponse {
  entity: { type: string; value: string };
  article_count: number;
  articles: PivotArticle[];
  related: PivotRelatedGroup[];
}

export async function fetchPivot(
  entityType: string,
  value: string,
  sinceHours = 0,
): Promise<PivotResponse> {
  const qs = new URLSearchParams({
    entity_type: entityType,
    value,
    since_hours: String(sinceHours),
    limit: "100",
  });
  const r = await fetch(`/api/v1/pivot?${qs.toString()}`, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`pivot failed: ${r.status}`);
  return (await r.json()) as PivotResponse;
}

// entity_type の日本語ラベルは backend vocab "entity_type" (vocabLabel) を SSoT に解決する
// (静的 value→label マップは持たない)。consumers: PivotResults / NewsPage / ArticleDetailPage。

export const PIVOT_ENTITY_TYPES: string[] = [
  "actor",
  "actor_provisional",
  "cve",
  "malware_family",
  "tool",
  "ttp",
  "ioc_ip",
  "ioc_domain",
  "ioc_url",
  "ioc_sha256",
  "ioc_sha1",
  "ioc_md5",
];
