/** 写し (Cloudflare Pages) 用の fetch 差し替え。
 *
 *  写しは **同じ API 面の静止画** なので、経路の付け替えは呼び出し元ごとではなく
 *  ここ 1 箇所で行う。個別に分岐を足していく方式だと、足し忘れた 1 本が
 *  「読み込み中のまま固まる」形で表に出る (2026-08-29 の実障害。語彙・チャンネル等
 *  7 本が素通りして起動関門が開かなかった)。
 *
 *  写していない API は **即座に失敗させる**。静的配信は存在しない path にも
 *  index.html を 200 で返すため、そのままだと JSON として壊れた応答を掴んで
 *  再試行を繰り返す。黙って待たせるより、失敗として扱う方が画面は正しく振る舞う。
 */
import { fileName } from "./mirrorStatic";

const DATA_BASE = import.meta.env.VITE_MIRROR_DATA || "/data";

function notMirrored(path: string): Response {
  return new Response(JSON.stringify({ detail: `写しに含まれていません: ${path}` }), {
    status: 501,
    headers: { "content-type": "application/json" },
  });
}

/** API の path → 写しのファイル。
 *
 *  書き出し側 (export_mirror.py) の置き場と **対で** 決まる。片方だけ変えると
 *  「取得できませんでした」の形で表に出る。
 *  一覧の絞り込みは写しでは効かない (条件ごとにファイルを持つと組み合わせ爆発する)
 *  ため、全件を返してブラウザ側で絞る。 */
async function locate(pathname: string): Promise<string | null> {
  const detail = pathname.match(/^\/api\/v1\/(articles|eventnews)\/([^/]+)$/);
  if (detail) {
    const id = decodeURIComponent(detail[2]);
    return `${DATA_BASE}/${detail[1]}/${await fileName(id)}.json`;
  }
  if (pathname === "/api/v1/articles") return `${DATA_BASE}/articles.json`;
  if (pathname === "/api/v1/eventnews") return `${DATA_BASE}/eventnews.json`;
  // 絞り込みを持たない参照データ (語彙・チャンネル等)。
  return `${DATA_BASE}/api/${await fileName(pathname)}.json`;
}

export function installMirrorFetch(): void {
  const original = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const path = url.startsWith("/") ? url : new URL(url, window.location.origin).pathname;
    if (!path.startsWith("/api/")) return original(input as RequestInfo, init);

    // 書き込みは写しには存在しない。試みさせず、その場で断る。
    const method = (init?.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    if (method !== "GET") return notMirrored(path);

    const pathname = path.split("?")[0];
    const file = await locate(pathname);
    if (!file) return notMirrored(pathname);
    const r = await original(file);
    // 静的配信の取りこぼしは 200 + HTML で返ってくる。中身で判定する。
    if (!r.ok || !(r.headers.get("content-type") || "").includes("json")) {
      return notMirrored(pathname);
    }
    return r;
  };
}
