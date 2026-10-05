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
import type { ArticleFeedItem, ArticleFeedResponse } from "./articles";

const DATA_BASE = import.meta.env.VITE_MIRROR_DATA || "/data";

function notMirrored(path: string): Response {
  return new Response(JSON.stringify({ detail: `この画面では表示できません: ${path}` }), {
    status: 501,
    headers: { "content-type": "application/json" },
  });
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });
}

/** 静止画でも読めるよう JSON content-type を見る。中身が壊れていれば使わない。 */
async function isJsonResponse(r: Response): Promise<boolean> {
  return r.ok && (r.headers.get("content-type") || "").includes("json");
}

// backend src/ui/api/articles_feed.py `_CATEGORY_GROUPS` と同じ合成カテゴリ。
// ここがずれると widget の絞り込みと写しの絞り込みが別物になる。
const ARTICLES_CATEGORY_GROUPS: Record<string, string[]> = {
  vuln: ["vulnerability", "advisory"],
  threat: ["malware", "apt", "apt_leak", "phishing"],
  incident_breach: ["incident", "breach"],
};

// この集合 **だけ** をブラウザ側で絞り込む。ここに無いキー (malware/cve/intent/pir/
// actor/affected_vendor/body 等) が指定されたら、黙って全件を返すのではなく
// 501 にして表に出す (実測: 30 件のはずが 6,443 件出ていた、という事故を再発させない)。
// "search" (2026-10-04 検索 UX 統一): ニュース検索の「キーワード」モードはこの口
// (/api/v1/articles?search=) を通る。ライブは title/summary/body の文字列一致だが、
// 写しは本文を持たないため title/summary (+タグ) で近似する (events の
// mirrorEventsFilter.ts の見出し+要点検索と同じ割り切り)。
const ARTICLES_FALLBACK_SUPPORTED = new Set([
  "status", "category", "channel", "importance", "feed", "jp", "level_filter",
  "min_severity", "relevant_only", "include_strategic", "sort", "search",
  "since_hours", "since", "limit", "offset", "include_summary",
]);

/** 写しのキーワード検索。title/summary + malware_families (タグ) の部分一致。
 *  ライブは body まで見るため、写しの結果はライブの部分集合になる (events と同じ割り切り)。 */
function matchesArticleSearch(a: ArticleFeedItem, term: string): boolean {
  const n = term.trim().toLowerCase();
  if (!n) return true;
  const haystack = `${a.title} ${a.summary ?? ""} ${(a.malware_families ?? []).join(" ")}`.toLowerCase();
  return haystack.includes(n);
}

// 重要度 6 段階 (1 が最上位・未記録は null)。null は常に最後に回す。
function levelSortValue(a: ArticleFeedItem): number {
  return a.level == null ? Number.POSITIVE_INFINITY : a.level;
}

function matchesImportance(a: ArticleFeedItem, importance: string): boolean {
  // backend と同じ意味論: "medium" は medium 以上 (medium+high) を含む。
  if (importance === "medium") return a.importance === "medium" || a.importance === "high";
  return a.importance === importance;
}

function matchesCategory(a: ArticleFeedItem, category: string): boolean {
  const group = ARTICLES_CATEGORY_GROUPS[category];
  return group ? group.includes(a.category ?? "") : a.category === category;
}

// 日本との関係 (2026-10-04)。backend と同じ意味論: "mentioned" は言及以上
// (targeted/affected/mentioned、= jp <> "none")。
function matchesJp(a: ArticleFeedItem, jp: string): boolean {
  if (jp === "targeted_affected") return a.jp === "targeted" || a.jp === "affected";
  if (jp === "mentioned") return Boolean(a.jp) && a.jp !== "none";
  return true;
}

// 深刻さ facet の許容値 → severity の許容集合 (常に上位からの prefix)。
// SSoT: src/storage/importance_level_sql.py:SEVERITY_ALLOWED。
const SEVERITY_ALLOWED: Record<string, ("S3" | "S2" | "S1")[]> = {
  S3: ["S3"],
  S2: ["S3", "S2"],
  S1: ["S3", "S2", "S1"],
};

/** 深刻さ・関連性・戦略上の重みの 3 独立 facet (2026-10-04)。backend
 *  (src/storage/importance_level_sql.py:severity_relevance_sql) と同じ意味論を
 *  severity/relevant/strategic_weight から再現する。旧 1 本化 ``level_filter``
 *  (top/notable/relevant) も後方互換で受け付け、3 facet が無指定のときだけ写像する。 */
function matchesSeverityFacet(
  a: ArticleFeedItem,
  params: URLSearchParams,
): boolean {
  let minSeverity = params.get("min_severity") ?? "";
  let relevantOnly = params.get("relevant_only") === "1";
  let includeStrategic = params.get("include_strategic") === "1";
  const levelFilter = params.get("level_filter");
  if (!minSeverity && !relevantOnly && !includeStrategic && levelFilter) {
    if (levelFilter === "top") {
      minSeverity = "S3";
    } else if (levelFilter === "notable") {
      minSeverity = "S2";
      includeStrategic = true;
    } else if (levelFilter === "relevant") {
      minSeverity = "S1";
      relevantOnly = true;
    }
  }
  if (!minSeverity && !relevantOnly) return true;
  if (!minSeverity) return a.relevant === true;
  const allowed = SEVERITY_ALLOWED[minSeverity] ?? [];
  let core = a.severity != null && allowed.includes(a.severity);
  if (includeStrategic) {
    core = core || (a.severity == null && a.strategic_weight === "heavy");
  }
  if (relevantOnly) core = core && a.relevant === true;
  return core;
}

function withinSinceHours(a: ArticleFeedItem, sinceHours: number): boolean {
  const at = a.published_at ?? a.created_at;
  if (!at) return false;
  return new Date(at).getTime() >= Date.now() - sinceHours * 60 * 60 * 1000;
}

function withinSinceIso(a: ArticleFeedItem, sinceIso: string): boolean {
  const at = a.created_at;
  if (!at) return false;
  const since = Date.parse(sinceIso);
  return !Number.isNaN(since) && new Date(at).getTime() >= since;
}

/** `/api/v1/articles` の絞り込み付き取得を、全件写し (articles.json) から
 *  ブラウザ側で再現する。exact query 用のファイルを写していない組み合わせ
 *  (widget の config 次第で limit/category/importance 等が自由に変わる) を
 *  救うための経路。対応していない絞り込みが混ざっていたら null を返し、
 *  呼び出し元が 501 にする (誤ったデータを黙って返さない)。
 *  `loadArticles` は呼び出し元 (`installMirrorFetch`) ごとに 1 つ持つ全件写しの
 *  取得・保持を注入する (install 単位でキャッシュを分けないとテスト間で汚染する)。 */
async function fallbackArticles(
  search: string,
  loadArticles: () => Promise<ArticleFeedResponse>,
): Promise<Response | null> {
  const params = new URLSearchParams(search);
  for (const key of params.keys()) {
    if (!ARTICLES_FALLBACK_SUPPORTED.has(key)) return null;
  }

  // 写し自体が status=posted (backend 既定) で書き出されているため、他の status は救えない。
  const status = params.get("status") ?? "posted";
  if (status !== "posted") return null;

  const { articles } = await loadArticles();
  let items = articles;

  const category = params.get("category");
  if (category) items = items.filter((a) => matchesCategory(a, category));

  const channel = params.get("channel");
  if (channel) items = items.filter((a) => a.posted_channel === channel);

  const importance = params.get("importance");
  if (importance) items = items.filter((a) => matchesImportance(a, importance));

  const feed = params.get("feed");
  if (feed) items = items.filter((a) => a.feed_title === feed);

  const jp = params.get("jp");
  if (jp) items = items.filter((a) => matchesJp(a, jp));

  if (
    params.has("level_filter") ||
    params.has("min_severity") ||
    params.has("relevant_only") ||
    params.has("include_strategic")
  ) {
    items = items.filter((a) => matchesSeverityFacet(a, params));
  }

  const sinceHours = Number(params.get("since_hours") || "0");
  if (sinceHours > 0) items = items.filter((a) => withinSinceHours(a, sinceHours));

  const since = params.get("since");
  if (since) items = items.filter((a) => withinSinceIso(a, since));

  // title/summary + タグの部分一致 (summary 剥がし前、2026-10-04)。
  const searchTerm = params.get("search");
  if (searchTerm) items = items.filter((a) => matchesArticleSearch(a, searchTerm));

  // 並び順は既定で書き出し元 (created_at DESC) のまま保つ。絞り込みは順序を変えない。
  // sort=level (2026-10-04) は重要度 6 段階の高い順 (未記録は最後) に並べ直す。
  if (params.get("sort") === "level") {
    items = [...items].sort((a, b) => levelSortValue(a) - levelSortValue(b));
  }
  const offset = Number(params.get("offset") || "0");
  const limit = Number(params.get("limit") || "20");
  const page = items.slice(offset, offset + limit);

  const includeSummary = params.get("include_summary") === "1" || params.get("include_summary") === "true";
  const resultArticles = includeSummary ? page : page.map((a) => ({ ...a, summary: null }));

  return jsonResponse({ articles: resultArticles, count: resultArticles.length });
}

/** API の path → 写しのファイル。
 *
 *  書き出し側 (export_mirror.py) の置き場と **対で** 決まる。片方だけ変えると
 *  「取得できませんでした」の形で表に出る。
 *  一覧の絞り込みは写しでは効かない (条件ごとにファイルを持つと組み合わせ爆発する)
 *  ため、全件を返してブラウザ側で絞る。 */
async function locate(pathname: string, search: string): Promise<string | null> {
  const detail = pathname.match(/^\/api\/v1\/(articles|eventnews)\/([^/]+)$/);
  if (detail) {
    const id = decodeURIComponent(detail[2]);
    return `${DATA_BASE}/${detail[1]}/${await fileName(id)}.json`;
  }
  // ⚠ 一覧の**全件ファイル**を絞り込みごとに別経路として無条件に使い回すと、
  //    画面は黙って違うものを出す (実測: 30 件のはずが 6,443 件出ていた)。
  //    絞り込み付きの記事一覧は `fallbackArticles` が対応範囲を判定してから
  //    全件写しを絞り込む。対応外の組み合わせはそこで 501 相当に落ちる。
  if (!search) {
    if (pathname === "/api/v1/articles") return `${DATA_BASE}/articles.json`;
    if (pathname === "/api/v1/eventnews") return `${DATA_BASE}/eventnews.json`;
  }
  return null;
}

export function installMirrorFetch(): void {
  const original = window.fetch.bind(window);

  // install 単位で握る (TTL なし、SPA の 1 読み込みの中で何度も絞り込みが変わるだけ
  // なので握りっぱなしで十分。長時間開いたタブで古くなる懸念は articles.json 自体の
  // 書き出し間隔 (3 時間) と同じ程度で、他の静的データとも揃っている)。
  let articlesJsonCache: Promise<ArticleFeedResponse> | null = null;
  function loadArticlesJson(): Promise<ArticleFeedResponse> {
    if (!articlesJsonCache) {
      articlesJsonCache = original(`${DATA_BASE}/articles.json`).then(
        (r) => r.json() as Promise<ArticleFeedResponse>,
      );
      articlesJsonCache.catch(() => {
        articlesJsonCache = null;
      });
    }
    return articlesJsonCache;
  }

  window.fetch = async (input, init) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    // 絶対 URL でも問い合わせ文字列を落とさない (落とすと絞り込みの写しに当たらない)。
    const u = new URL(url, window.location.origin);
    const path = u.pathname + u.search;
    if (!path.startsWith("/api/")) return original(input as RequestInfo, init);

    // 書き込みは写しには存在しない。試みさせず、その場で断る。
    const method = (init?.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    if (method !== "GET") return notMirrored(path);

    // 絞り込みごとに別ファイルとして写しているので、**問い合わせ文字列まで含めて**
    // 引く。無ければ path だけで引き直す (時刻など毎回変わる引数を持つ経路のため)。
    const [pathname, search] = path.split("?");
    const named = await locate(pathname, search);
    const candidates = named
      ? [named]
      : [
          ...(search ? [`${DATA_BASE}/api/${await fileName(path)}.json`] : []),
          `${DATA_BASE}/api/${await fileName(pathname)}.json`,
        ];
    for (const file of candidates) {
      const r = await original(file);
      // 静的配信の取りこぼしは 200 + HTML で返ってくる。中身で判定する。
      if (await isJsonResponse(r)) return r;
    }

    // 絞り込み付きの記事一覧は、専用ファイルが無くても全件写しから引き直せる
    // (dashboard widget の config は per/mode/category/importance 等で自由に変わり、
    // 組み合わせを全部書き出すのは組み合わせ爆発になる)。
    if (pathname === "/api/v1/articles" && search) {
      const fallback = await fallbackArticles(search, loadArticlesJson);
      if (fallback) return fallback;
    }
    return notMirrored(path);
  };
}
