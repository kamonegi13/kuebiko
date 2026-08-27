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

/** 続報で本文がどう動いたか。
 *
 *  - `rewritten`: 新しい事実が加わり、本文を書き直した (経緯を `revisions` で出せる)
 *  - `follow_up`: 他媒体が同じ内容を報じた (裏取りが増えただけ・本文は不変)
 *  - `null`: 初報のまま
 *
 *  「更新」と称して中身が変わっていないと読み手は差分を探して見つけられないので、
 *  この 2 つを同じバッジにしない。2026-08-27 より前の静的書き出しには無い。 */
export type PublicUpdateKind = "rewritten" | "follow_up" | null;

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
  /** 最終報の時刻 (一覧の並び順と一致する)。 */
  published_at: string;
  /** 初報の時刻。続報で `published_at` が動いたときに「いつの事象か」を示す。 */
  first_reported_at?: string;
  update_kind?: PublicUpdateKind;
  /** 本文を書き直した最後の時刻 (`update_kind === "rewritten"` のときのみ)。 */
  updated_at?: string | null;
  /** 内容は変えずに後から報じた媒体数。 */
  follow_up_sources?: number;
  citations: PublicCitation[];
}

/** 続報 1 件の経緯。**本文の行単位の差分ではない** — 本文は合流のたびに全面的に
 *  書き直されるため、版どうしの文面比較では内容の異同を測れない (実測で同じ事実を
 *  述べた 2 版が 12 行中 11 行「新規」と出た)。ここに出るのは合流判定が決定論で
 *  記録した「加わった要素」だけ。 */
export interface PublicNewsRevision {
  at: string;
  added: { type: string; label: string; values: string[] }[];
  /** 事実そのものではない変化 (裏取りの増加・一次情報源の登場・重要度の引き上げ)。 */
  note: string;
  source: string;
  url: string;
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
  /** 拾い読み用の箇条書き (backend の `list[str]`)。出典番号は持たない。
   *  2026-08-26 より前の版には無いので、空配列を必ず既定にする。 */
  key_points: string[];
  facts: PublicNewsFact[];
  /** ⚠ facts と同じ形 (オブジェクト)。string[] ではない — 2026-08-25 に取り違えて
   *  公開サイトを真っ黒にした (React は object を子に渡すと throw する)。 */
  discrepancies: PublicNewsFact[];
  /** 原文が自ら付けた但し書き (「これは投稿数であり被害数ではない」等)。
   *  2026-08-27 より前の版には無いので、空配列を必ず既定にする。 */
  caveats?: PublicNewsFact[];
  /** こちらは素の文字列 (backend の `list[str]`)。 */
  unknowns: string[];
  published_at: string;
  first_reported_at: string;
  update_kind?: PublicUpdateKind;
  updated_at?: string | null;
  follow_up_sources?: number;
  /** 古い順。2026-08-27 より前の静的書き出しには無いので既定を空配列にする。 */
  revisions?: PublicNewsRevision[];
  independent_sources: number;
  citations: PublicCitation[];
  note: string;
}

import {
  fetchPublicMapStatic,
  fetchPublicNewsDetailStatic,
  fetchPublicNewsStatic,
} from "./publicNewsStatic";

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

/** 静的配信 (Cloudflare Pages) では書き出した JSON を読む。
 *
 * ビルド時に決まる。**呼び手は分岐を知らない** — 画面側に if を撒くと、
 * 片方の経路だけ壊れても気付けない。 */
const STATIC = import.meta.env.VITE_PUBLIC_STATIC === "1";

export function fetchPublicNews(q: PublicNewsQuery = {}) {
  if (STATIC) return fetchPublicNewsStatic(q);
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
  if (STATIC) return fetchPublicNewsDetailStatic(id);
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
  if (STATIC) return fetchPublicMapStatic();
  return get<PublicMapResponse>(`/api/v1/public/news/map?days=${days}`);
}
