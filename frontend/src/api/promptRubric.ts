// summarizer プロンプトの層分け (WP3): rubric 構造化エディタの型 + API client。
// JSON 形は設計書 (summarizer プロンプトの層分け) §5 API 契約が正 — バックエンド
// (WP1/WP2、別エージェントが並行実装中) の実装ではなくこの契約に従ってコードを書く。
// getJson/postJson の慣行は pages.ts に合わせる (エラーは detail を Error として throw)。

const BASE = "/api/v1/prompts/summarizer/rubric";

async function getJson<T>(path: string): Promise<T> {
  const r = await fetch(path, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`HTTP ${r.status}: ${path}`);
  return r.json() as Promise<T>;
}

async function sendJson<T>(path: string, method: "POST" | "PUT", body: unknown): Promise<T> {
  const r = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: "same-origin",
  });
  if (!r.ok) {
    let detail = `HTTP ${r.status}`;
    try {
      const j = (await r.json()) as { detail?: string };
      if (j.detail) detail = j.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return r.json() as Promise<T>;
}

// ===== §2-1 / §5 の型 =====

export type RubricSectionKind = "rubric" | "suppressed";

export interface RubricSection {
  field_id: string;
  kind: RubricSectionKind;
  title: string;
  body: string;
  note: string;
}

export interface RubricExample {
  label: string;
  json_text: string;
}

export interface SummarizerRubric {
  schema_version: number;
  intro: string;
  sections: RubricSection[];
  examples: RubricExample[];
}

// schema (SummaryOutput) 由来の契約メタ。UI 表示専用、プロンプトには出さない。
// フロントに型表を複製しないためのラベル SSoT (§6-3 の「型/必須/enum バッジ」の元)。
// 宣言状況との突合 (unknown/missing) は preview 応答が一元的に持つ (GET 側に重複させない)。
export interface ContractField {
  name: string;
  json_type: string;
  required: boolean;
  nullable: boolean;
  enum: string[];
}

export interface RubricContract {
  fields: ContractField[];
}

export interface RuntimeInfo {
  composer_enabled: boolean;
  active_source: "composed" | "legacy_file";
  fallback_reason: string;
  legacy_path: string;
  version: number | null;
  saved_at: string;
}

// ===== UI 再設計 (rubric guide / issues / field-stats) の追加型。設計書 §2/§3/§4 が正。 =====

// group の日本語ラベル・説明・表示順は useVocabOptions("rubric_group") から解決する
// (I-5: ラベル SSoT)。ここではキー空間の型だけを持つ。
export type RubricGroupId =
  | "classification"
  | "victim"
  | "technical"
  | "narrative"
  | "suppressed"
  | "other";

// 1 フィールドの「効く先」ガイド。SSoT は backend `src/prompts/rubric_guide.py`。
export interface RubricFieldGuide {
  field_id: string;
  group: RubricGroupId;
  order: number;
  effect: string;
  /** false = 下流が必ず上書きするため、本文を書いても捨てられる (入力欄を出さない)。
   *  SSoT は backend の SUMMARIZER_OVERRIDDEN_FIELDS。 */
  editable?: boolean;
  sources: string[];
}

export interface RubricGuide {
  fields: RubricFieldGuide[];
  unmapped_fields: string[];
}

export type RubricIssueSeverity = "error" | "warning";
export type RubricIssueCode =
  | "duplicate_field_id"
  | "unknown_field"
  | "empty_rubric_body"
  | "example_json_invalid"
  | "example_not_object"
  | "example_schema_mismatch"
  | "render_failed"
  | "missing_fields"
  | "composed_longer_than_legacy";

// 保存前検証 (preview) の 1 件。target=section のとき field_id、target=example のとき
// example_index (1-based) が立つ。どちらも立たなければグローバル (intro / 全体構成) の issue。
export interface RubricIssue {
  severity: RubricIssueSeverity;
  code: RubricIssueCode;
  message: string;
  field_id: string;
  example_index: number;
  keys: string[];
}

export type StatsDays = 7 | 30 | 90;
export type FieldStatAvailability = "full" | "partial" | "none";

export interface StatBucket {
  value: string;
  label: string | null;
  vocab: string | null;
  count: number;
  share: number;
  drilldown_query: string | null;
}

export interface StatSubMetric {
  key: string;
  label: string | null;
  vocab: string | null;
  filled: number;
  total: number;
  rate: number;
  note: string;
}

export interface StatCoverage {
  filled: number;
  total: number;
  rate: number;
  scope_label: string;
}

export interface StatAverage {
  label: string;
  value: number;
  unit: string;
}

export interface FieldStat {
  field_id: string;
  availability: FieldStatAvailability;
  source_note: string;
  notes: string[];
  coverage: StatCoverage | null;
  distribution: StatBucket[];
  sub_metrics: StatSubMetric[];
  average: StatAverage | null;
}

export interface FieldStatsResponse {
  window: { days: number; since: string; until: string };
  denominator: { label: string; count: number; note: string };
  generated_at: string;
  fields: Record<string, FieldStat>;
}

export interface RubricGetResponse {
  rubric: SummarizerRubric;
  contract: RubricContract;
  runtime: RuntimeInfo;
  guide: RubricGuide;
}

export interface RubricSaveResponse {
  saved: boolean;
  version: number;
  warnings: string[];
  composed_chars: number;
  legacy_chars: number;
}

export interface PreviewResult {
  valid: boolean;
  errors: string[];
  warnings: string[];
  unknown_fields: string[];
  missing_fields: string[];
  composed: string;
  composed_chars: number;
  legacy_chars: number;
  rendered_sample: string;
  issues: RubricIssue[];
}

export interface TestResult {
  ok: boolean;
  model: string;
  elapsed_ms: number;
  prompt_chars: number;
  output: Record<string, unknown> | null;
  empty_fields: string[];
  error: string | null;
}

export const promptRubricApi = {
  // GET /api/v1/prompts/summarizer/rubric
  getRubric: () => getJson<RubricGetResponse>(BASE),

  // PUT /api/v1/prompts/summarizer/rubric — note 省略時はバックエンド既定 ("UI 編集") に委ねる
  saveRubric: (rubric: SummarizerRubric, note?: string) =>
    sendJson<RubricSaveResponse>(BASE, "PUT", { rubric, note: note ?? "" }),

  // POST /api/v1/prompts/summarizer/rubric/preview — rubric 省略 = 現行保存値
  previewRubric: (rubric?: SummarizerRubric) =>
    sendJson<PreviewResult>(`${BASE}/preview`, "POST", rubric !== undefined ? { rubric } : {}),

  // POST /api/v1/prompts/summarizer/rubric/test — rubric/articleId 省略 = 現行保存値 / 固定サンプル記事
  testRubric: (params: { rubric?: SummarizerRubric | null; articleId?: string | null } = {}) =>
    sendJson<TestResult>(`${BASE}/test`, "POST", {
      rubric: params.rubric ?? null,
      article_id: params.articleId ?? null,
    }),

  // GET /api/v1/prompts/summarizer/rubric/field-stats?days=N — 直近 N 日の実出力統計。
  // 保存では invalidate しない (統計は「その時々の基準で生成された履歴」— 設計書 §4)。
  getFieldStats: (days: StatsDays) => getJson<FieldStatsResponse>(`${BASE}/field-stats?days=${days}`),
};
