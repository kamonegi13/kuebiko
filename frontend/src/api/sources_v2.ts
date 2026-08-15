// 統合 Source API client (全 transport: rss/sitemap/html_scraper)。
// backend: src/ui/api/sources.py (/api/v1/sources/*)。

async function postJson<TReq, TRes>(path: string, body: TReq): Promise<TRes> {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: "same-origin",
  });
  if (!r.ok) {
    const text = await r.text();
    throw new Error(`HTTP ${r.status}: ${text}`);
  }
  return (await r.json()) as TRes;
}

export type Transport = "rss" | "atom" | "sitemap" | "html_scraper";

// 1 ソースの編集可能フィールド。transport 固有のものは該当しないと null
// (UI はその欄自体を出さない)。backend: src/ui/api/_source_editor.py。
export interface EditableSource {
  feed_id: string;
  transport: Transport;
  display_name: string;
  url: string;
  folder: string;
  enabled: boolean;
  article_link_selector: string | null;
  url_include_pattern: string | null;
  max_posts_per_run: number | null;
  // rss のみ true = URL が識別子。変更すると運用統計の連続性が切れる。
  url_is_identity: boolean;
}

export interface SourceUpdate {
  feed_id: string;
  url?: string;
  folder?: string;
  enabled?: boolean;
  article_link_selector?: string;
  url_include_pattern?: string;
  max_posts_per_run?: number;
}

export interface PreviewArticle {
  title: string;
  url: string;
  published: string | null;
  summary_preview: string;
}

export interface Quality {
  entries_count: number;
  freshness_days: number | null;
  health_score: number;
}

export interface SourceCandidate {
  transport: Transport;
  fetch_url: string;
  source_name: string;
  path_display: string;
  last_updated: string | null;
  quality: Quality;
  preview_articles: PreviewArticle[];
  detected_via: string;
  article_link_selector?: string | null;
  title_selector?: string | null;
  // sitemap のみ: 取り込む URL の絞り込みと、その候補となるパス区分。
  url_include_pattern?: string | null;
  path_hints?: string[];
  rationale?: string | null;
}

export interface DiscoverResponse {
  candidates: SourceCandidate[];
  html_scrape_eligible: boolean;
  notes: string[];
  original_url: string;
}

export interface PreviewHtmlListingResponse {
  candidate: SourceCandidate | null;
  error: string | null;
}

export interface LivePreviewResponse {
  ok: boolean;
  kind: string;
  checked_url: string;
  items: PreviewArticle[];
  error: string | null;
  // どの段で取得できたか。"browser" = bot UA がブロックされており、本番の
  // 定期取得も同じエスカレーションで動く (プレビュー = 本番と同一取得層)
  fetch_stage?: "bot" | "browser";
}

export interface RegisterRequest {
  candidate: SourceCandidate;
  name: string;
  folder: string;
  enabled: boolean;
}

export interface RegisterResponse {
  added: boolean;
  transport: Transport;
  target_yaml: string;
  total_in_yaml: number;
  appended_to_cluster: boolean;
  commit: string | null;
}

export const sourcesV2Api = {
  discover: (url: string, max_preview_per_candidate = 5) =>
    postJson<{ url: string; max_preview_per_candidate: number }, DiscoverResponse>(
      "/api/v1/sources/discover",
      { url, max_preview_per_candidate },
    ),
  previewHtmlListing: (listing_url: string, hint = "") =>
    postJson<{ listing_url: string; hint: string }, PreviewHtmlListingResponse>(
      "/api/v1/sources/preview_html_listing",
      { listing_url, hint },
    ),
  previewHtmlListingExplicit: (
    listing_url: string,
    article_link_selector: string,
    title_selector = "",
    url_include_pattern = "",
  ) =>
    postJson<
      {
        listing_url: string;
        article_link_selector: string;
        title_selector: string;
        url_include_pattern: string;
      },
      PreviewHtmlListingResponse
    >("/api/v1/sources/preview_html_listing_explicit", {
      listing_url,
      article_link_selector,
      title_selector,
      url_include_pattern,
    }),
  // visual picker iframe 用の same-origin proxy URL を組み立てる。
  proxyPageUrl: (listing_url: string, render: "auto" | "static" | "js" = "auto") =>
    `/api/v1/sources/proxy_page?url=${encodeURIComponent(listing_url)}&render=${render}`,
  // 登録済みソースの最新情報をライブ取得 (機能しているか確認)。
  livePreview: (feed_id: string) =>
    postJson<{ feed_id: string }, LivePreviewResponse>(
      "/api/v1/sources/live_preview",
      { feed_id },
    ),
  // feed_id で全 transport (rss/scraper/watcher) を削除。
  deleteSource: (feed_id: string) =>
    postJson<{ feed_id: string }, { removed: boolean; commit: string | null; error: string | null }>(
      "/api/v1/sources/delete",
      { feed_id },
    ),
  // 全 transport 横断の一括 enable/disable/delete。
  bulk: (feed_ids: string[], action: "enable" | "disable" | "delete") =>
    postJson<
      { feed_ids: string[]; action: string },
      { affected: number; commit: string | null; error: string | null }
    >("/api/v1/sources/bulk", { feed_ids, action }),
  // 全 transport 横断の folder 設定 (空文字で解除)。
  setFolder: (feed_ids: string[], folder: string) =>
    postJson<
      { feed_ids: string[]; folder: string },
      { affected: number; commit: string | null; error: string | null }
    >("/api/v1/sources/set_folder", { feed_ids, folder }),
  // 保存前の取得テスト (編集中の URL をその場で取得)。html_scraper は
  // セレクタと一体で確認する必要があるため previewHtmlListingExplicit を使う。
  previewUrl: (url: string, kind: "rss" | "sitemap", url_include_pattern = "") =>
    postJson<
      { url: string; kind: string; url_include_pattern: string },
      LivePreviewResponse
    >("/api/v1/sources/preview_url", { url, kind, url_include_pattern }),
  // 実際に使われているフォルダ一覧 (分類候補の SSoT。固定リストは stale 化する)。
  folders: async (): Promise<{ folders: string[] }> => {
    const r = await fetch("/api/v1/sources/folders", { credentials: "same-origin" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return (await r.json()) as { folders: string[] };
  },
  // 1 ソースの編集可能フィールド (取得設定フォームの初期値)。
  getEditable: async (feed_id: string): Promise<EditableSource> => {
    const r = await fetch(`/api/v1/sources/editable?feed_id=${encodeURIComponent(feed_id)}`, {
      credentials: "same-origin",
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
    return (await r.json()) as EditableSource;
  },
  // 1 ソースの取得設定を更新 (未指定 = 変更しない)。rss は URL 変更で feed_id も変わる。
  update: (patch: SourceUpdate) =>
    postJson<SourceUpdate, { updated: boolean; feed_id: string; error: string | null }>(
      "/api/v1/sources/update",
      patch,
    ),
  setDisplayName: (feed_id: string, display_name: string) =>
    postJson<
      { feed_id: string; display_name: string },
      { affected: number; commit: string | null; error: string | null }
    >("/api/v1/sources/set_display_name", { feed_id, display_name }),
  register: (req: RegisterRequest) =>
    postJson<RegisterRequest, RegisterResponse>("/api/v1/sources/register", req),
};
