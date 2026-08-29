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
import { STATIC_TTL_MS, ttlCached } from "./ttlCache";

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
  /** カテゴリ key → 表示名。表示名の SSoT は backend で、静的配信では
   *  語彙 API を読めないため書き出しに同梱される。 */
  category_labels?: Record<string, string>;
}

/** 読み込んだ索引に含まれるカテゴリ表示名 (同期アクセス用)。
 *  一覧を読む前は空なので、呼び手は素の key へ落ちること。 */
export const staticCategoryLabels: Record<string, string> = {};

async function json<T>(path: string): Promise<T> {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return (await r.json()) as T;
}

// 期限付きで握る。無期限だと画面が取り直しても通信が起きず、再読込するまで
// 永久に古い記事が出る (2026-08-29 利用者指摘の「自動で更新されない」の正体)。
const loadIndex = ttlCached(
  () =>
    json<StaticIndex>(`${DATA_BASE}/index.json`).then((data) => {
      Object.assign(staticCategoryLabels, data.category_labels ?? {});
      return data;
    }),
  STATIC_TTL_MS,
);

// 全文は重いので、最初に検索したときだけ取りに行く。中身は索引より変わらないが、
// 同じ理由で期限は付ける。
const loadSearch = ttlCached(
  () => json<Record<string, string>>(`${DATA_BASE}/search.json`),
  STATIC_TTL_MS,
);

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
