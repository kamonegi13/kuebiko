import { fetchEventNewsStatic, fetchEventNewsDetailStatic } from "./mirrorStatic";
/** 事象ニュースの一覧行。裏取りは independent_sources で表す (member_count ではない)。 */
export interface EventNewsListItem {
  id: string;
  headline: string;
  preview: string;
  status: "new" | "updated" | "reinforced" | "dormant";
  change_kind: "add" | "correct" | null;
  importance: string;
  member_count: number;
  independent_sources: number;
  state_media_count: number;
  unclassified_sources: number;
  best_source_tier: string;
  first_reported_at: string;
  last_reported_at: string;
  current_version: number;
  has_news: boolean;
}

export interface EventNewsFact {
  text: string;
  source_index: number;
  paragraph: number;
  /** 節の key (SECTION_KEYS)。2026-08-26 より前の版は持たない。 */
  section?: string;
}

export interface EventNewsMember {
  index: number;
  article_id: string;
  title: string;
  url: string;
  feed_title: string;
  source_tier: string;
  /** 発信者種別 (Grok/X の account_class)。空 = 未分類 / 非 X。 */
  account_class: string;
  published_at: string | null;
  summary: string;
  joined_at: string;
  contributed_new_facts: boolean;
}

export interface EventNewsFacet {
  key: string;
  /** label = 表示名、vocab = 値のラベル解決に使う語彙名 (backend が指定)。 */
  label: string;
  vocab: string;
  values: { value: string; articles: number }[];
}

/** 記事画面の「Diamond / 判定」と同じ行を出すための集計。 */
export interface EventNewsJudgement {
  intent?: EventNewsFacet | null;
  intent_confidence?: { value: string; articles: number }[];
  texts?: { label: string; items: { text: string; source_index: number }[] }[];
  stance?: EventNewsFacet | null;
  victim_sector?: EventNewsFacet | null;
  victim_country?: EventNewsFacet | null;
  channel?: EventNewsFacet | null;
  category?: EventNewsFacet | null;
  pmesii?: { axis: string; articles: number }[];
}

/** 原記事から抽出済みのメタデータ (決定論の集約。LLM を通らない)。 */
export interface EventNewsMetadata {
  entities: {
    type: string;
    values: { value: string; articles: number }[];
    omitted: number;
    cvss?: Record<string, { score: number; severity: string }>;
    affected?: Record<string, { vendors: string[]; products: string[] }>;
  }[];
  subject_actors: { id: string; label: string; articles: number }[];
  facets: EventNewsFacet[];
  judgement: EventNewsJudgement;
}

export interface EventNewsDetail {
  id: string;
  status: string;
  change_kind: string | null;
  importance: string;
  independent_sources: number;
  state_media_count: number;
  unclassified_sources: number;
  best_source_tier: string;
  first_reported_at: string;
  last_reported_at: string;
  news: {
    version: number;
    generated_at: string;
    model: string;
    headline: string;
    bluf: string;
    /** 拾い読み用の要点。公開面と同じ版を読むので中身は共通。 */
    key_points?: string[];
    facts: EventNewsFact[];
    discrepancies: EventNewsFact[];
    /** 原文が自ら付けた但し書き。 */
    caveats?: EventNewsFact[];
    unknowns: string[];
    dropped_lines: number;
    resolved_ids: number;
    /** 構成記事から抽出済みの固有情報 (CVE・アクター・マルウェア・被害組織・製品・ベンダ) のうち
     *  要約に書かれていないもの。機械的な文字列照合 (LLM なし)。照合対象が無ければ null。 */
    fidelity?: { checked: number; missing: { type: string; value: string }[] } | null;
    history: { version: number; generated_at: string }[];
  } | null;
  members: EventNewsMember[];
  /** 裏取りの内訳 = 媒体単位の tier 分布 (記事数ではない)。 */
  corroboration: { tier: string; media: number }[];
  metadata: EventNewsMetadata;
  /** 「別事象だが関連」(分割の由来)。parent = この事象が外れてきた本体。 */
  related: { parent: RelatedEvent | null; children: RelatedEvent[] };
  note: string;
}

export interface RelatedEvent {
  id: string;
  headline: string;
  member_count: number;
  last_reported_at: string;
}

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return (await r.json()) as T;
}

/** 一覧の絞り込み。記事側の条件はニュース検索と同じ語彙 (同じ経路で解決される)。 */
export interface EventNewsQuery {
  limit?: number;
  offset?: number;
  importance?: string;
  search?: string;
  category?: string;
  channel?: string;
  /** 情報源 (feed_title)。記事側 facet をそのまま持ち上げる。 */
  feed?: string;
  actor?: string;
  cve?: string;
  malware?: string;
  intent?: string;
  pir?: string;
  affected_vendor?: string;
  entity_type?: string;
  entity_value?: string;
  since_hours?: number;
  /** 事象固有: 独立媒体数の下限 (2 = 複数媒体が報じた事象のみ)。0 = 絞らない。 */
  min_independent_sources?: number;
  /** 事象固有: 生成済み (統合記事あり) だけに絞る。未指定 = 絞らない。 */
  has_news?: boolean;
  /** 事象固有: 状態 (new / updated / reinforced) のカンマ区切り。 */
  status?: string;
  /** 意味検索を併用する (言い換え・多言語を拾う)。既定 off。 */
  semantic?: boolean;
}

/** 写し (Cloudflare Pages) から読むか。ビルド時に決まる。
 *  **呼び手は分岐を知らない** — 画面に if を撒くと片方の経路だけ壊れても気付けない。 */
const MIRROR = import.meta.env.VITE_MIRROR === "1";

export function fetchEventNews(q: EventNewsQuery = {}) {
  if (MIRROR) return fetchEventNewsStatic(q.limit ?? 50);
  const p = new URLSearchParams({ limit: String(q.limit ?? 50) });
  if (q.offset) p.set("offset", String(q.offset));
  if (q.since_hours) p.set("since_hours", String(q.since_hours));
  if (q.min_independent_sources) p.set("min_independent_sources", String(q.min_independent_sources));
  // false も意味を持つ (未生成のみ) ため undefined とだけ区別する
  if (q.has_news !== undefined) p.set("has_news", String(q.has_news));
  if (q.semantic) p.set("semantic", "true");
  for (const k of [
    "importance", "search", "category", "channel", "feed", "actor", "cve",
    "malware", "intent", "pir", "affected_vendor", "entity_type", "entity_value", "status",
  ] as const) {
    const v = q[k];
    if (v) p.set(k, String(v));
  }
  return get<{ items: EventNewsListItem[]; note: string; scan_capped?: boolean }>(
    `/api/v1/eventnews?${p}`,
  );
}

export function fetchEventNewsDetail(id: string) {
  if (MIRROR) return fetchEventNewsDetailStatic(id);
  return get<EventNewsDetail>(`/api/v1/eventnews/${encodeURIComponent(id)}`);
}

/** 事象単位の memo / bookmark。記事単位のメモとは粒度が違う (事象は記事の集合)。 */
export interface EventNote {
  item_id: string;
  bookmarked: boolean;
  note: string;
  tags: string[];
  judgment: string;
  updated_at?: string | null;
}

/** 事象から導いた関係 (2026-09-27)。同じ出来事の関連 = 分類器 / 同じアクター = 主題アクターの共有 */
export interface DerivedEventRelation {
  item_id: string;
  headline: string;
  rel_type: string;
  label: string;
  role: "a" | "b"; // この事象が先 (a) か後 (b) か。包含は a = まとめの側
  basis: string[]; // 画面用の文言 (「被害組織: …」「攻撃者: …」)
  confidence: number | null; // 同じ出来事の関連の確率 (同じアクターは決定論なので null)
}

export function fetchEventRelations(itemId: string) {
  return get<{ relations: DerivedEventRelation[] }>(
    `/api/v1/eventnews/${encodeURIComponent(itemId)}/relations`,
  );
}

export function fetchEventNote(itemId: string) {
  return get<EventNote>(`/api/v1/event-notes/${encodeURIComponent(itemId)}`);
}

export async function saveEventNote(
  itemId: string,
  body: Pick<EventNote, "bookmarked" | "note" | "tags" | "judgment">,
): Promise<EventNote> {
  const r = await fetch(`/api/v1/event-notes/${encodeURIComponent(itemId)}`, {
    method: "PUT",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return (await r.json()) as EventNote;
}
