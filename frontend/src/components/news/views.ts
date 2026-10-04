// ニュースの絞り込み「ビュー」(名前付きの絞り込みの組)。
// docs/news_filter_ux.md §3-5。ニュース検索・事象ニュース・ダッシュボードの
// 記事フィード/事象ニュース widget が共有する。
//
// ``NewsFilters`` はページの URL クエリとは別の、ビュー保存専用の中間表現。
// 各画面は自分の state ⇄ NewsFilters の変換を自前で持つ (News は "since"、
// 事象ニュースは "since_hours" のように URL param 名が画面ごとに違うため、
// ここでは画面に依存しない語彙だけを定義する)。

export interface NewsFilters {
  search: string;
  minSeverity: "" | "S3" | "S2" | "S1";
  relevantOnly: boolean;
  includeStrategic: boolean;
  jp: "" | "targeted_affected" | "mentioned";
  /** 期間 (時間)。"0" = 全期間。文字列で持つ (URL クエリとの往復がそのままできる)。 */
  since: string;
  sort: "" | "level";
  category: string;
  feed: string;
  intent: string;
  pir: string;
  actor: string;
  affectedVendor: string;
  /** 本文の有無。ニュース検索のみで使う ("" / "full" / "stump")。 */
  body: string;
  /** 購読チャンネル。運用画面のみ (写しには出さない)。 */
  channel: string;
  /** 事象ニュース専用の軸 (ニュース検索では常に false)。 */
  minIndependentSources: boolean;
  hasNews: boolean;
  newFactsOnly: boolean;
}

export const EMPTY_FILTERS: NewsFilters = {
  search: "",
  minSeverity: "",
  relevantOnly: false,
  includeStrategic: false,
  jp: "",
  since: "0",
  sort: "",
  category: "",
  feed: "",
  intent: "",
  pir: "",
  actor: "",
  affectedVendor: "",
  body: "",
  channel: "",
  minIndependentSources: false,
  hasNews: false,
  newFactsOnly: false,
};

export interface NewsView {
  id: string;
  label: string;
  builtin?: boolean;
  filters: Partial<NewsFilters>;
}

/** 既定のビュー (docs/news_filter_ux.md §3 の表)。category の値は
 *  components/news/facets.tsx の CATEGORY_OPTION / useFacetOptions と同じ実 id。 */
export const BUILTIN_VIEWS: readonly NewsView[] = [
  { id: "builtin:focus", label: "注目", builtin: true, filters: { minSeverity: "S2", relevantOnly: true } },
  { id: "builtin:japan", label: "日本", builtin: true, filters: { jp: "targeted_affected" } },
  { id: "builtin:critical", label: "重大", builtin: true, filters: { minSeverity: "S3" } },
  { id: "builtin:vuln", label: "脆弱性", builtin: true, filters: { category: "vuln", minSeverity: "S2" } },
  { id: "builtin:nationstate", label: "国家系", builtin: true, filters: { relevantOnly: true, category: "apt" } },
  {
    id: "builtin:geopolitical",
    label: "地政学",
    builtin: true,
    filters: { category: "geopolitical", includeStrategic: true },
  },
  // ダッシュボードの事象ニュース widget の旧既定 (registry.tsx defaultConfig:
  // {min_severity: "S2"}) を view として表せるようにする (2026-10-04 追補)。
  { id: "builtin:notable", label: "注意以上", builtin: true, filters: { minSeverity: "S2" } },
  { id: "builtin:all", label: "すべて", builtin: true, filters: {} },
] as const;

/** view の絞り込みを完全な ``NewsFilters`` にする (未指定項目は既定値)。
 *  ビュー適用 = 「いまの絞り込みを全部リセットしてから view.filters を重ねる」。 */
export function viewEffectiveFilters(view: NewsView): NewsFilters {
  return { ...EMPTY_FILTERS, ...view.filters };
}

/** 2 つの ``NewsFilters`` が完全一致するか (view の「変更あり」判定に使う)。 */
export function filtersEqual(a: NewsFilters, b: NewsFilters): boolean {
  return (Object.keys(EMPTY_FILTERS) as (keyof NewsFilters)[]).every((k) => a[k] === b[k]);
}

/** いまの絞り込みに一致する view の id (完全一致するものが無ければ null)。 */
export function matchingViewId(filters: NewsFilters, views: readonly NewsView[]): string | null {
  for (const v of views) {
    if (filtersEqual(filters, viewEffectiveFilters(v))) return v.id;
  }
  return null;
}

/** 利用者保存ビューの id として使える値か (``builtin:`` 接頭辞は予約)。 */
export function isUserViewId(id: string): boolean {
  return id.length > 0 && !id.startsWith("builtin:");
}

/** ラベルから保存用 id を作る (英数字以外は "-" に畳む。重複時は呼び手が連番を振る)。 */
export function slugifyViewId(label: string): string {
  const rand = Math.random().toString(36).slice(2, 8);
  const base = `view-${Date.now().toString(36)}-${rand}`;
  const hint = label
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 24);
  return hint ? `${base}-${hint}` : base;
}

/** NewsFilters (部分) を NewsPage (``/app/news``) の URL クエリへ変換する。
 *  ダッシュボードの記事フィード widget のヘッダーリンクが使う。 */
export function toNewsPageHref(f: Partial<NewsFilters>): string {
  const q = new URLSearchParams();
  if (f.category) q.set("category", f.category);
  if (f.feed) q.set("feed", f.feed);
  if (f.channel) q.set("channel", f.channel);
  if (f.minSeverity) q.set("min_severity", f.minSeverity);
  if (f.relevantOnly) q.set("relevant_only", "1");
  if (f.minSeverity && f.includeStrategic) q.set("include_strategic", "1");
  if (f.jp) q.set("jp", f.jp);
  if (f.since && f.since !== "0") q.set("since", f.since);
  if (f.sort) q.set("sort", f.sort);
  const qs = q.toString();
  return qs ? `/app/news?${qs}` : "/app/news";
}

/** NewsFilters (部分) を EventNewsPage (``/app/eventnews``) の URL クエリへ変換する。
 *  News ページと param 名が違う (``since`` → ``since_hours``) ので別関数にする。 */
export function toEventNewsPageHref(f: Partial<NewsFilters>): string {
  const q = new URLSearchParams();
  if (f.category) q.set("category", f.category);
  if (f.feed) q.set("feed", f.feed);
  if (f.channel) q.set("channel", f.channel);
  if (f.minSeverity) q.set("min_severity", f.minSeverity);
  if (f.relevantOnly) q.set("relevant_only", "1");
  if (f.minSeverity && f.includeStrategic) q.set("include_strategic", "1");
  if (f.jp) q.set("jp", f.jp);
  if (f.since && f.since !== "0") q.set("since_hours", f.since);
  if (f.sort) q.set("sort", f.sort);
  if (f.minIndependentSources) q.set("min_sources", "2");
  if (f.hasNews) q.set("has_news", "1");
  if (f.newFactsOnly) q.set("status", "updated");
  const qs = q.toString();
  return qs ? `/app/eventnews?${qs}` : "/app/eventnews";
}
