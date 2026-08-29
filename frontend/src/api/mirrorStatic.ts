// 運用画面の「写し」からの読み取り (Cloudflare Pages 配信用)。
//
// Mac に到達できないときの継続が目的。ライブの代わりではなく **その時点の写し**で、
// 書き出しは scripts/export_ops_mirror.py が稼働中の API を叩いて作る。
//
// ⚠ 呼び手は経路を知らない。分岐は各 api/*.ts の入口 1 箇所に置き、画面側に
//    if を撒かない (片方の経路だけ壊れても気付けなくなる)。
import type { ArticleFeedResponse } from "./articles";
import type { EventNewsDetail, EventNewsListItem } from "./eventnews";

/** 書き出したデータの置き場 (ページの基底に対する相対)。 */
const DATA_BASE = import.meta.env.VITE_MIRROR_DATA || "/data";

/** 写しの素性。画面はこれを読んで「○○時点の写し」を常時出す。
 *  無いとライブと見分けが付かず、いつの情報を見ているか判断できない。 */
export interface MirrorMeta {
  generated_at: string;
  kind: "ops-mirror";
  window_days: number;
  /** 記事本文を含むか。false なら詳細の body は空文字で、
   *  「取得に失敗した」ではなく「写していない」を意味する。 */
  with_bodies: boolean;
  counts: { articles: number; eventnews: number };
}

async function getJson<T>(path: string): Promise<T> {
  const r = await fetch(`${DATA_BASE}${path}`, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return (await r.json()) as T;
}

/** id → ファイル名。書き出し側 (_safe_name) と **同じ規則**でなければ引けない。
 *  記事 id は URL をそのまま使う経路があるため、ハッシュに落として桁を揃える。 */
async function fileName(id: string): Promise<string> {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(id));
  return [...new Uint8Array(buf)]
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("")
    .slice(0, 32);
}

let metaCache: Promise<MirrorMeta> | null = null;

/** 写しの素性を読む (1 回だけ取りに行く)。 */
export function fetchMirrorMeta(): Promise<MirrorMeta> {
  metaCache ??= getJson<MirrorMeta>("/meta.json");
  return metaCache;
}

let articlesCache: Promise<ArticleFeedResponse> | null = null;

/** 記事一覧。**絞り込みはブラウザ側で行う** — 写しは 1 ファイルなので、
 *  条件ごとに別ファイルを持つと組み合わせ爆発する。 */
export async function fetchArticlesStatic(limit = 200): Promise<ArticleFeedResponse> {
  articlesCache ??= getJson<ArticleFeedResponse>("/articles.json");
  const all = await articlesCache;
  return { articles: all.articles.slice(0, limit), count: all.count };
}

export async function fetchArticleDetailStatic(articleId: string): Promise<unknown> {
  return getJson(`/articles/${await fileName(articleId)}.json`);
}

let eventsCache: Promise<{ items: EventNewsListItem[] }> | null = null;

export async function fetchEventNewsStatic(
  limit = 50,
): Promise<{ items: EventNewsListItem[]; note: string; scan_capped?: boolean }> {
  eventsCache ??= getJson<{ items: EventNewsListItem[] }>("/eventnews.json");
  const all = await eventsCache;
  // scan_capped は「走査を打ち切った」ことを示すライブ側の事情。写しには無い。
  return { items: all.items.slice(0, limit), note: "" };
}

export async function fetchEventNewsDetailStatic(id: string): Promise<EventNewsDetail> {
  return getJson<EventNewsDetail>(`/eventnews/${await fileName(id)}.json`);
}
