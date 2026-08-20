// プロンプト層分けの一般化 (2026-08-20): block 方式プロンプト (status_synthesis 以降) の
// 型 + API client。backend `src/ui/api/prompt_blocks.py` が正 — 契約はそこから読む。
//
// block 方式は summarizer (field_rubric 方式) と **同一の pydantic model
// (SummarizerRubric)** を編集層に使う (src/prompts/registry.py の設計判断)。そのため
// rubric/examples の形は promptRubric.ts の RubricSection/RubricExample と完全一致する
// ので再利用する (型を複製しない)。ただし block 方式では:
//   - `sections[].field_id` は SummaryOutput のフィールド名ではなく **block id**
//     (skeleton の `{# BLOCK: <id> #}` マーカーに対応する)
//   - `intro` は block_composer.compose_blocks が一切参照しない (skeleton marker 置換のみ)
//     ため常に無効 — 編集 UI を出さない (BlockPromptEditor 側の判断)
//   - `examples` は backend `validate_blocks` が非空を拒否するため常に空

import type { RubricExample, RubricSection } from "./promptRubric";

// fetch ヘルパは promptRubric.ts と同じ慣行 (エラーは detail を Error として throw)。
// pages.ts とは別 module なので共有せず、この api file 内で完結させる (既存の慣行踏襲)。
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

export type ManagedPromptKind = "field_rubric" | "block";

export interface ManagedPrompt {
  prompt_id: string;
  title: string;
  kind: ManagedPromptKind;
  config_key: string;
  /** rollback 用の据置 .j2 のパス (raw 一覧との突合で据置バナーを出す)。 */
  legacy_path: string;
  version: number | null;
  saved_at: string;
}

export interface ManagedPromptsResponse {
  prompts: ManagedPrompt[];
}

// block 方式の編集層。SummarizerRubric と構造は同一だが、summarizer 側の型名をそのまま
// import 元へ書くと block 方式の呼び出し側で意味が読みにくいためエイリアスする。
export interface BlockRubric {
  schema_version: number;
  intro: string;
  sections: RubricSection[];
  examples: RubricExample[];
}

export interface BlockRuntimeInfo {
  composer_enabled: boolean;
  active_source: "composed" | "legacy_file";
  fallback_reason: string;
  legacy_path: string;
  env_flag: string;
  version: number | null;
  saved_at: string;
}

export interface BlocksGetResponse {
  prompt_id: string;
  title: string;
  rubric: BlockRubric;
  // プロンプト内の出現順 (skeleton のマーカー順)。カードはこの順で描く — DB の
  // sections 配列順ではない (そちらは I-1 と同じ理由で保存時に一切並べ替えない)。
  slots: string[];
  runtime: BlockRuntimeInfo;
}

export interface BlocksSaveResponse {
  saved: boolean;
  version: number;
}

export interface BlocksPreviewResult {
  valid: boolean;
  // block API はカード単位の局在化情報 (field_id 等) を持たない文字列 list のみ返す
  // (§検証: slot/block 1:1 → Jinja 構文 → サンプル render)。UI はバナー表示に留める。
  errors: string[];
  composed: string;
  composed_chars: number;
  rendered_sample: string;
}

function blocksBase(promptId: string): string {
  return `/api/v1/prompts/${encodeURIComponent(promptId)}/blocks`;
}

export const promptBlocksApi = {
  // GET /api/v1/prompts/managed
  managed: () => getJson<ManagedPromptsResponse>("/api/v1/prompts/managed"),

  // GET /api/v1/prompts/{prompt_id}/blocks
  getBlocks: (promptId: string) => getJson<BlocksGetResponse>(blocksBase(promptId)),

  // PUT /api/v1/prompts/{prompt_id}/blocks — note 省略時はバックエンド既定 ("UI 編集") に委ねる
  saveBlocks: (promptId: string, rubric: BlockRubric, note?: string) =>
    sendJson<BlocksSaveResponse>(blocksBase(promptId), "PUT", { rubric, note: note ?? "" }),

  // POST /api/v1/prompts/{prompt_id}/blocks/preview — rubric 省略 = 現行保存値
  previewBlocks: (promptId: string, rubric?: BlockRubric) =>
    sendJson<BlocksPreviewResult>(
      `${blocksBase(promptId)}/preview`,
      "POST",
      rubric !== undefined ? { rubric } : {},
    ),
};
