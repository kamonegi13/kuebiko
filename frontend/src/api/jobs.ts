// 統一バックグラウンドジョブ制御プレーンの API client。
// K1 scheduled pipelines / K2 bespoke jobs / K3 reactive triggers を
// 1 つの Job 抽象として list / toggle / reschedule / run する。
// backend contract: GET /jobs, POST /jobs/{id}/toggle|schedule|run

const BASE = "/api/v1/jobs";

export type JobKind = "pipeline" | "bespoke" | "reactive";
export type JobProtection = "critical" | "important" | "optional";
export type JobScheduleType = "cron" | "interval" | "reactive";

export interface JobLastRun {
  last_run_at: string;
  status: string;
  detail: string;
}

export interface JobView {
  id: string;
  kind: JobKind;
  title: string;
  description: string;
  disable_impact: string;
  protection: JobProtection;
  enabled: boolean;
  schedule_type: JobScheduleType;
  hour: number | null;
  minute: number | null;
  day_of_week: string | null;
  day: string | null;
  interval_minutes: number | null;
  offset_minutes: number | null;
  debounce_hours: number | null;
  schedule_label: string;
  next_run_at: string | null;
  is_paused: boolean | null;
  last_run: JobLastRun | null;
  danger_note: string | null;
  // 収集ジョブか。true = 発火が active な heavy 処理の run 区間に重なると抑止される
  // (現状 direct-rss-fetch / web-scraper-watchers)。
  respects_analysis_window: boolean;
  // 重い LLM ジョブか。true = timeline に黄色い「重処理」帯を出す。この帯は収集停止ゾーンでもある
  // (morning/evening-brief, synthesis 系, weekly-recap, taxonomy/mitre 同期, pir-* 等)。
  heavy: boolean;
  // 定常キュー処理 (upkeep) か。true = タイムラインで専用 swimlane を持たず
  // 「定常処理」集約 1 行に畳まれる (SSoT は backend JobDef.upkeep)。
  upkeep?: boolean;
  // 想定処理時間 (分)。timeline の Gantt バー幅に使う (monthly=45 が最長、非heavy=5)。
  max_runtime_minutes: number;
}

// 「重い処理を避けたい時刻帯」(JST)。timeline に shaded band として描画する。
// job_id/day_of_week/day/max_runtime_minutes は後方互換で追加された
// (現行 UI はバンドを jobs から動的導出するため未使用)。
export interface DangerWindow {
  hour: number;
  minute: number;
  label: string;
  job_id?: string;
  day_of_week?: string | null;
  day?: string | null;
  max_runtime_minutes?: number;
}

export interface JobsResponse {
  jobs: JobView[];
  scheduler_available: boolean;
  disabled_important: string[];
  danger_windows: DangerWindow[];
}

export interface TogglePatch {
  enabled: boolean;
  confirm?: boolean;
}

// reschedule は任意の subset を送れる。number 系は null 不可 (未指定 = キー省略)。
export interface SchedulePatch {
  schedule_type?: JobScheduleType;
  hour?: number;
  minute?: number;
  day_of_week?: string;
  day?: string;
  interval_minutes?: number;
  offset_minutes?: number;
  debounce_hours?: number;
}

export interface ToggleResult {
  ok: boolean;
  enabled: boolean;
}

export interface ScheduleResult {
  ok: boolean;
  schedule_label: string;
  danger_note: string | null;
}

export interface RunResult {
  ok: boolean;
  triggered_at: string;
}

// 実行履歴の 1 件 (詳細パネル用)。pipeline はリッチ (posted/fetched/error)、
// bespoke/reactive は最小 (metrics は null)。finished_at は running/bespoke で null。
export interface JobRunRecord {
  started_at: string;
  finished_at: string | null;
  status: string;
  posted: number | null;
  total_fetched: number | null;
  error_count: number | null;
  detail: string;
}

export interface JobRunsResponse {
  job_id: string;
  kind: JobKind;
  runs: JobRunRecord[];
}

// non-2xx は {detail} を読んで Error message に載せる (409/400/404/503 の分岐は呼び出し側)。
// status も message 先頭に付与して呼び出し側で 409 pre-gate 等を判定できるようにする。
export class JobApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(detail);
    this.name = "JobApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function readDetail(r: Response): Promise<string> {
  try {
    const j = (await r.json()) as { detail?: string };
    if (j && typeof j.detail === "string" && j.detail) return j.detail;
  } catch {
    /* non-JSON error body */
  }
  return `HTTP ${r.status}`;
}

async function getJson<T>(path: string): Promise<T> {
  const r = await fetch(path, { credentials: "same-origin" });
  if (!r.ok) throw new JobApiError(r.status, await readDetail(r));
  return (await r.json()) as T;
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
    credentials: "same-origin",
  });
  if (!r.ok) throw new JobApiError(r.status, await readDetail(r));
  return (await r.json()) as T;
}

export function listJobs(): Promise<JobsResponse> {
  return getJson<JobsResponse>(BASE);
}

export function toggleJob(id: string, enabled: boolean, confirm?: boolean): Promise<ToggleResult> {
  const body: TogglePatch = { enabled };
  if (confirm) body.confirm = true;
  return postJson<ToggleResult>(`${BASE}/${encodeURIComponent(id)}/toggle`, body);
}

export function rescheduleJob(id: string, patch: SchedulePatch): Promise<ScheduleResult> {
  return postJson<ScheduleResult>(`${BASE}/${encodeURIComponent(id)}/schedule`, patch);
}

export function runJobNow(id: string): Promise<RunResult> {
  return postJson<RunResult>(`${BASE}/${encodeURIComponent(id)}/run`, {});
}

// 選択ジョブの実行履歴 (新しい順)。詳細パネルの一覧表示用。
export function listJobRuns(id: string, limit = 20): Promise<JobRunsResponse> {
  return getJson<JobRunsResponse>(
    `${BASE}/${encodeURIComponent(id)}/runs?limit=${limit}`,
  );
}
