// /api/v1/runs + /api/v1/dashboard 用の小さい client。

export interface RunBrief {
  id: number;
  started_at: string | null;
  finished_at: string | null;
  pipeline: string;
  dry_run: boolean;
  triggered_by: string;
  status: string;
  total_fetched: number;
  summarized: number;
  posted: number;
  marked_read: number;
  error_count: number;
  note: string | null;
  log_line_count: number;
  log_truncated: boolean;
}

export interface PipelineOption {
  name: string;
  source_type: string;
}

export interface RecentRunsResponse {
  running: RunBrief[];
  recent: RunBrief[];
}

export interface DashboardSummary {
  days: number;
  summary: {
    total_runs: number;
    posted_total: number;
    extract_failure_rate: number;
    // 切り株率 (2026-07-27): 全文取得失敗→feed 抜粋 fallback の割合
    stump_body_rate?: number;
    importance: Record<string, number>;
    daily_posts: [string, number][];
  };
  recent_runs: RunBrief[];
  next_run_at: string | null;
  db_stats: Record<string, number>;
}

export interface LogLine {
  seq: number;
  ts: string;
  stream: string;
  line: string;
}

export interface RunLogResponse {
  run_id: number;
  status: string;
  lines: LogLine[];
}

async function getJson<T>(path: string): Promise<T> {
  const r = await fetch(path, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`HTTP ${r.status}: ${path}`);
  return (await r.json()) as T;
}

export const runsApi = {
  pipelines: () => getJson<{ pipelines: PipelineOption[] }>("/api/v1/runs/pipelines"),
  recent: (limit = 10) => getJson<RecentRunsResponse>(`/api/v1/runs/recent?limit=${limit}`),
  get: (id: number) => getJson<RunBrief>(`/api/v1/runs/${id}`),
  log: (id: number, from = 0) =>
    getJson<RunLogResponse>(`/api/v1/runs/${id}/log?from_seq=${from}`),
  start: async (form: FormData): Promise<{ run_id: number }> => {
    const r = await fetch("/api/v1/runs/start", {
      method: "POST",
      body: form,
      credentials: "same-origin",
    });
    if (!r.ok) {
      const text = await r.text();
      throw new Error(`HTTP ${r.status}: ${text}`);
    }
    return r.json() as Promise<{ run_id: number }>;
  },
};

export const dashboardApi = {
  summary: (days = 7) =>
    getJson<DashboardSummary>(`/api/v1/dashboard/summary?days=${days}`),
};
