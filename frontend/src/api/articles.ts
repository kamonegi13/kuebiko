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
  // 日本との関係 (2026-10-04、article_importance_v2.jp)。記録前の記事は null。
  jp?: "targeted" | "affected" | "mentioned" | "none" | null;
  // 重要度 6 段階 (1 が最上位) と深刻さ (S3/S2/S1)。記録前の記事は null (2026-10-04)。
  level?: number | null;
  severity?: "S3" | "S2" | "S1" | null;
  // 軸なし (severity=null) 記事の戦略上の重み。旧 level_filter="notable" /
  // include_strategic の client 側再現 (ミラー) 用 (2026-10-04)。
  strategic_weight?: "heavy" | "moderate" | "light" | null;
  // 関連性あり (日本・注視国・SIR)。severity が無い記事でも立つので、level の奇偶だけ
  // では再現できない (relevant_only の client 側再現用、2026-10-04)。
  relevant?: boolean | null;
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
  // 日本との関係: "targeted_affected"=標的・被害 / "mentioned"=言及以上。未指定=絞らない。
  jp?: "targeted_affected" | "mentioned";
  // 重要度 6 段階による絞り込み (旧 1 本化 facet、後方互換のみ)。"top"=重大のみ /
  // "notable"=注意以上 / "relevant"=関連性ありのみ。未指定=絞らない。
  // **非推奨** — 新規コードは下の 3 独立 facet (min_severity/relevant_only/
  // include_strategic) を使う (「関連性ありの重大」のように level_filter では
  // 表せない組み合わせがあるため、2026-10-04 に置き換えた)。
  level_filter?: "top" | "notable" | "relevant";
  // 深刻さ: ""(すべて) / "S3"(重大のみ) / "S2"(注意以上) / "S1"(参考以上)。
  min_severity?: "" | "S3" | "S2" | "S1";
  // 関連性ありのみ (深刻さの設定と独立)。
  relevant_only?: boolean;
  // 政策・地政学を含める (min_severity 指定時のみ意味を持つ)。
  include_strategic?: boolean;
  // 並び順。"level"=重要度 6 段階の高い順 (未記録は最後)。未指定=新しい順 (既定)。
  sort?: "level";
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
    if (params.jp) q.set("jp", params.jp);
    if (params.level_filter) q.set("level_filter", params.level_filter);
    if (params.min_severity) q.set("min_severity", params.min_severity);
    if (params.relevant_only) q.set("relevant_only", "1");
    if (params.include_strategic) q.set("include_strategic", "1");
    if (params.sort) q.set("sort", params.sort);
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
