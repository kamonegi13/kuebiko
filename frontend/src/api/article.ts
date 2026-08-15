// Phase 2 K4: 記事 deep-view API (全 enrichment + Discord deep-link)。

export interface ArticlePmesii {
  p: boolean;
  m: boolean;
  e: boolean;
  s: boolean;
  i_infra: boolean;
  i_cyber: boolean;
  p_env: boolean;
  t: boolean;
}

export interface ArticleDetail {
  article_id: string;
  title: string;
  url: string;
  feed_title: string | null;
  importance: string | null;
  category: string | null;
  status: string | null;
  posted_channel: string | null;
  // flow Phase 3: 「なぜこのチャンネルか」(投稿先決定の監査情報、route() 非経由なら null)。
  routing_rule_id: string | null;
  // 現行ルールセットで解決した表示名 (2026-08-15)。削除済みルール由来なら null。
  routing_rule_label: string | null;
  routing_reason: string | null;
  summary: string | null;
  body: string | null;
  // 本文オンデマンド日本語訳キャッシュ (2026-07-25)。null=未訳 (翻訳ボタンを出す)。
  body_ja: string | null;
  victim_sector: string | null;
  victim_sector_raw: string | null;
  victim_country: string | null;
  victim_country_raw: string | null;
  socio_political_intent: string | null;
  intent_confidence: string | null; // null=旧レジーム確定 / low=仮説 (H3 配線)
  socio_political_rationale: string | null;
  technical_axis_summary: string | null;
  remediation: string | null; // 本文明示の対処 1 文 (M1: write-only 解消)
  editorial_stance: string | null;
  pmesii: ArticlePmesii;
  discord_message_id: string | null;
  discord_channel_id: string | null;
  published_at: string | null;
  created_at: string | null;
  // 時間軸レイヤ b/c: 事象の実発生(検知/公表)日を報道時刻と分離。
  event_date: string | null;
  event_date_basis: string | null; // occurred|detected|disclosed|reported|relative
  compromise_date: string | null;
  reporting_lag_days: number | null; // 報道日 - 発生日 (大=retrospective/続報)
  dwell_days: number | null; // 発生(検知)日 - 侵害開始日 (滞留時間)
  // 本文完全性 (2026-07-27): body の由来 (full_extract/playwright_extract/prefetch/scraper/
  // grok/feed_summary/none) と全文取得失敗理由。UI が「フィード抜粋のみ (全文未取得)」を明示。
  body_source: string | null;
  extraction_failure_reason: string | null;
  // 主題アクター層 (記事の主語=攻撃主体)。言及 (entity actor) と分離して役割表示する。
  // subject_actor_source='none'=評価済み・主題なし / null=未評価。
  subject_actor_ids: string[];
  subject_actors: { id: string; label: string }[];
  subject_actor_source: string | null;
  subject_actor_confidence: string | null;
  /** 主題判定の根拠 (LLM 層、2026-08-13)。「正しい未帰属」の理由を可視化する。 */
  subject_actor_rationale: string | null;
  // 記事タイプ (breaking/advisory/recap/…、judgment_classifier 由来)。
  article_type: string | null;
}

export interface CvssInfo {
  score: number;
  severity: string;
}

export interface AffectedInfo {
  vendors: string[];
  products: string[];
}

export interface ArticleEntityGroup {
  type: string;
  values: string[];
  cvss?: Record<string, CvssInfo>;
  // CVE の affected vendor/product (NVD CPE)。exposure 表示 + vendor facet 起点。
  affected?: Record<string, AffectedInfo>;
}

export interface ArticleNote {
  article_id: string;
  bookmarked: boolean;
  note: string;
  tags: string[];
  judgment: string;
  updated_at?: string | null;
}

export interface ArticleDetailResponse {
  article: ArticleDetail;
  discord_url: string | null;
  entities: ArticleEntityGroup[];
  note: ArticleNote | null;
}

// Phase 5: note の upsert (write、readonly instance では 403)。
export async function saveArticleNote(
  articleId: string,
  body: { bookmarked: boolean; note: string; tags: string[]; judgment: string },
): Promise<void> {
  const r = await fetch(`/api/v1/notes/${encodeURIComponent(articleId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`note save failed: ${r.status}`);
}

export interface NoteListItem extends ArticleNote {
  title: string;
  url: string;
  importance: string | null;
  category: string | null;
}

export async function fetchNotes(
  opts: { bookmarkedOnly?: boolean; tag?: string } = {},
): Promise<{ notes: NoteListItem[]; count: number }> {
  const qs = new URLSearchParams();
  if (opts.bookmarkedOnly) qs.set("bookmarked_only", "true");
  if (opts.tag) qs.set("tag", opts.tag);
  const r = await fetch(`/api/v1/notes?${qs.toString()}`, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`notes fetch failed: ${r.status}`);
  return (await r.json()) as { notes: NoteListItem[]; count: number };
}

export interface TranslateResponse {
  /** 完訳テキスト。partial=true の間は null。 */
  body_ja: string | null;
  cached: boolean;
  /** true = 時間上限で中断 (長文)。再 POST で続きから再開する。 */
  partial?: boolean;
  /** 先頭から連続して訳せている部分 (進捗表示用)。 */
  partial_text?: string;
  done_chunks?: number;
  total_chunks?: number;
}

// 本文オンデマンド日本語訳 (2026-07-25)。初回は LLM 翻訳 (本文長により数十秒〜数分)、
// 以後は body_ja キャッシュが即返る。readonly instance でも allowlist で許可される。
// 2026-08-06: 長文はサーバ側 120 秒でチャンク境界中断し partial を返す —
// 呼び出し側 (ArticleReadView) が完了まで自動で再 POST する (resumable)。
export async function translateArticle(articleId: string): Promise<TranslateResponse> {
  const r = await fetch(`/api/v1/articles/${encodeURIComponent(articleId)}/translate`, {
    method: "POST",
    credentials: "same-origin",
  });
  if (!r.ok) {
    const detail: unknown = await r
      .json()
      .then((d: { detail?: unknown }) => d?.detail)
      .catch(() => null);
    throw new Error(typeof detail === "string" ? detail : `翻訳に失敗しました (${r.status})`);
  }
  return (await r.json()) as TranslateResponse;
}

export async function fetchArticleDetail(articleId: string): Promise<ArticleDetailResponse> {
  const r = await fetch(`/api/v1/articles/${encodeURIComponent(articleId)}`, {
    credentials: "same-origin",
  });
  if (!r.ok) {
    if (r.status === 404) throw new Error("記事が見つかりません");
    throw new Error(`article fetch failed: ${r.status}`);
  }
  return (await r.json()) as ArticleDetailResponse;
}

// PMESII-PT 8 軸の表示ラベル。
export const PMESII_LABELS: { key: keyof ArticlePmesii; label: string }[] = [
  { key: "p", label: "政治 (P)" },
  { key: "m", label: "軍事 (M)" },
  { key: "e", label: "経済 (E)" },
  { key: "s", label: "社会 (S)" },
  { key: "i_infra", label: "情報-物理 (I)" },
  { key: "i_cyber", label: "情報-サイバー (I)" },
  { key: "p_env", label: "物理環境 (P)" },
  { key: "t", label: "時間 (T)" },
];
