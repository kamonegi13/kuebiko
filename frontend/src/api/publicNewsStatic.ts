// 静的配信 (Cloudflare Pages) 用のデータ取得。
//
// 読者を運用者の PC に依存させないため、公開面は書き出した JSON から読む。
// **絞り込みの所属は書き出し側が API に問い合わせて確定させている** —
// ここで `item.category === key` のように判定し直すと、サーバの意味 (構成記事の
// 分類で絞る) とずれる。表示上の category は代表値にすぎない。
//
// 検索は生成本文の全体が対象 (サーバの search_event_versions と同じ範囲)。
// 全文は重いので別ファイルにし、**最初に検索したときだけ**取りに行く。
import type {
  PublicMapResponse,
  PublicNewsDetail,
  PublicNewsItem,
  PublicNewsQuery,
} from "./publicNews";

/** 書き出したデータの置き場。ページの基底に対する相対。 */
const DATA_BASE = import.meta.env.VITE_PUBLIC_DATA || "/data";

interface StaticIndexItem extends PublicNewsItem {
  /** この記事が属する公開カテゴリ key (書き出し時に API が決めた)。 */
  cats: string[];
  /** 被害国 ISO (書き出し時に API が決めた)。 */
  countries: string[];
}

interface StaticIndex {
  items: StaticIndexItem[];
  categories: string[];
  featured: string[];
}

let indexPromise: Promise<StaticIndex> | null = null;
let searchPromise: Promise<Record<string, string>> | null = null;

async function json<T>(path: string): Promise<T> {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return (await r.json()) as T;
}

function loadIndex(): Promise<StaticIndex> {
  indexPromise ??= json<StaticIndex>(`${DATA_BASE}/index.json`);
  return indexPromise;
}

function loadSearch(): Promise<Record<string, string>> {
  searchPromise ??= json<Record<string, string>>(`${DATA_BASE}/search.json`);
  return searchPromise;
}

export async function fetchPublicNewsStatic(q: PublicNewsQuery = {}) {
  const data = await loadIndex();
  let items: StaticIndexItem[] = data.items;

  if (q.featured) {
    const rank = new Map(data.featured.map((id, i) => [id, i]));
    items = items.filter((i) => rank.has(i.id)).sort((a, b) => rank.get(a.id)! - rank.get(b.id)!);
  }
  if (q.category) items = items.filter((i) => i.cats.includes(q.category!));
  if (q.country) items = items.filter((i) => i.countries.includes(q.country!));
  if (q.search) {
    const needle = q.search.trim().toLowerCase();
    if (needle) {
      const haystacks = await loadSearch();
      items = items.filter((i) => (haystacks[i.id] ?? "").includes(needle));
    }
  }

  const offset = q.offset ?? 0;
  return {
    items: items.slice(offset, offset + (q.limit ?? 30)),
    note: "",
    categories: data.categories,
  };
}

export function fetchPublicNewsDetailStatic(id: string): Promise<PublicNewsDetail> {
  return json<PublicNewsDetail>(`${DATA_BASE}/news/${encodeURIComponent(id)}.json`);
}

export function fetchPublicMapStatic(): Promise<PublicMapResponse> {
  return json<PublicMapResponse>(`${DATA_BASE}/map.json`);
}
