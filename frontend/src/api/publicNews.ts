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
  /** 代表カテゴリ (構成記事の多数決)。一覧のバッジに使う。 */
  category: string;
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
  /** 節の種類 (what / scope / how / response / context / action)。
   *  v4 より前に生成された記事は持たない → 表示側は節見出し無しで描く。 */
  section?: string;
}

export interface PublicNewsDetail {
  id: string;
  headline: string;
  category: string;
  generated: boolean;
  bluf: string;
  facts: PublicNewsFact[];
  /** ⚠ facts と同じ形 (オブジェクト)。string[] ではない — 2026-08-25 に取り違えて
   *  公開サイトを真っ黒にした (React は object を子に渡すと throw する)。 */
  discrepancies: PublicNewsFact[];
  /** こちらは素の文字列 (backend の `list[str]`)。 */
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
  /** 被害国 ISO (地図からの遷移)。 */
  country?: string;
  /** 注目枠 = 複数媒体が報じ、かつ統合本文がある事象。 */
  featured?: boolean;
}

export function fetchPublicNews(q: PublicNewsQuery = {}) {
  const p = new URLSearchParams({ limit: String(q.limit ?? 30) });
  if (q.offset) p.set("offset", String(q.offset));
  if (q.search) p.set("search", q.search);
  if (q.category) p.set("category", q.category);
  if (q.country) p.set("country", q.country);
  if (q.featured) p.set("featured", "true");
  return get<{ items: PublicNewsItem[]; note: string; categories: string[] }>(
    `/api/v1/public/news?${p}`,
  );
}

export function fetchPublicNewsDetail(id: string) {
  return get<PublicNewsDetail>(`/api/v1/public/news/${encodeURIComponent(id)}`);
}

/** 公開地図の 1 国。 */
export interface PublicMapNode {
  iso: string;
  label: string;
  lat: number;
  lon: number;
  count: number;
}

export interface PublicMapResponse {
  nodes: PublicMapNode[];
  window_days: number;
  /** 地図に置けた件数 / 置けなかった件数 / 母集団。**割合を隠さない**ために全部返る。 */
  placed: number;
  unplaced: number;
  total: number;
  note: string;
}

export function fetchPublicMap(days = 30) {
  return get<PublicMapResponse>(`/api/v1/public/news/map?days=${days}`);
}
