// 記事フィード API client (ダッシュボード「記事フィード」widget 群の共通バックエンド)。
// backend: src/ui/api/articles_feed.py

export interface ArticleFeedItem {
  id: number | null;
  article_id: string;
  title: string;
  url: string;
  feed_title: string | null;
  importance: string | null;
  category: string | null;
  posted_channel: string | null;
  victim_sector: string | null;
  victim_country: string | null;
  // Diamond Model meta-feature 軸 (Phase Diamond-Axes)
  socio_political_intent: string | null; // Adversary⇄Victim の意図 (closed enum)
  intent_confidence?: string | null; // null=旧レジーム確定 / low=仮説 (H3 配線)
  technical_axis_summary: string | null; // Capability⇄Infrastructure の技術的結線
  malware_families: string[];
  summary: string | null;
  published_at: string | null;
  created_at: string | null;
}

export interface ArticleFeedResponse {
  articles: ArticleFeedItem[];
  count: number;
}

export interface ArticleFeedParams {
  importance?: string;
  category?: string;
  feed?: string;
  channel?: string;
  search?: string;
  malware?: string;
  cve?: string;
  intent?: string;
  pir?: string;
  actor?: string;
  affected_vendor?: string;
  body?: string; // "stump"=切り株(全文未取得) / "full"=全文取得済
  status?: string;
  since_hours?: number;
  since?: string; // W2: 「前回確認以降」カーソル (ISO 絶対時刻)。あれば since_hours より優先。
  limit?: number;
  include_summary?: boolean;
}

export const articlesApi = {
  list: (params: ArticleFeedParams = {}): Promise<ArticleFeedResponse> => {
    const q = new URLSearchParams();
    if (params.importance) q.set("importance", params.importance);
    if (params.category) q.set("category", params.category);
    if (params.feed) q.set("feed", params.feed);
    if (params.channel) q.set("channel", params.channel);
    if (params.search) q.set("search", params.search);
    if (params.malware) q.set("malware", params.malware);
    if (params.cve) q.set("cve", params.cve);
    if (params.intent) q.set("intent", params.intent);
    if (params.pir) q.set("pir", params.pir);
    if (params.actor) q.set("actor", params.actor);
    if (params.affected_vendor) q.set("affected_vendor", params.affected_vendor);
    if (params.body) q.set("body", params.body);
    if (params.status) q.set("status", params.status);
    if (params.since_hours) q.set("since_hours", String(params.since_hours));
    if (params.since) q.set("since", params.since);
    if (params.limit) q.set("limit", String(params.limit));
    if (params.include_summary) q.set("include_summary", "1");
    return fetch(`/api/v1/articles?${q.toString()}`, { credentials: "same-origin" }).then((r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json() as Promise<ArticleFeedResponse>;
    });
  },
};
