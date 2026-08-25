// 公開ニュース (Tier0)。匿名の第三者が読む唯一の面。
//
// **内部の api/eventnews.ts と混ぜない** — あちらは分析者向けで判定メタデータまで
// 返す。公開面は「出典必須・出版社の本文は返さない」という別契約なので型も分ける。

export interface PublicCitation {
  index: number;
  /** 原記事のタイトル (引用)。 */
  title: string;
  /** 原記事 URL。読み手はここへ辿る。 */
  url: string;
  /** 媒体名。 */
  source: string;
  source_tier: string;
  published_at: string | null;
}

export interface PublicNewsItem {
  id: string;
  headline: string;
  /** 統合済みなら生成 BLUF、単独報なら kuebiko が書いた要約。 */
  summary: string;
  /** kuebiko が複数媒体から生成した本文を持つか。 */
  generated: boolean;
  sources: number;
  independent_sources: number;
  published_at: string;
  citations: PublicCitation[];
}

export interface PublicNewsFact {
  text: string;
  source_index: number;
  paragraph: number;
}

export interface PublicNewsDetail {
  id: string;
  headline: string;
  generated: boolean;
  bluf: string;
  facts: PublicNewsFact[];
  discrepancies: string[];
  unknowns: string[];
  published_at: string;
  first_reported_at: string;
  independent_sources: number;
  citations: PublicCitation[];
  note: string;
}

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return (await r.json()) as T;
}

export interface PublicNewsQuery {
  limit?: number;
  offset?: number;
  search?: string;
  /** カテゴリ key。一覧レスポンスの `categories` が正 (フロントで定義を持たない)。 */
  category?: string;
  /** 注目枠 = 複数媒体が報じ、かつ統合本文がある事象。 */
  featured?: boolean;
}

export function fetchPublicNews(q: PublicNewsQuery = {}) {
  const p = new URLSearchParams({ limit: String(q.limit ?? 30) });
  if (q.offset) p.set("offset", String(q.offset));
  if (q.search) p.set("search", q.search);
  if (q.category) p.set("category", q.category);
  if (q.featured) p.set("featured", "true");
  return get<{ items: PublicNewsItem[]; note: string; categories: string[] }>(
    `/api/v1/public/news?${p}`,
  );
}

export function fetchPublicNewsDetail(id: string) {
  return get<PublicNewsDetail>(`/api/v1/public/news/${encodeURIComponent(id)}`);
}
