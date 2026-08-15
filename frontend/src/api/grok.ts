// Grok セッション管理 API client (2026-07-05)。
// 状態可視化 → 再取得ガイド → 完了自動検知 → 検証 の一気通貫フロー用。

export interface GrokStateSummary {
  exists: boolean;
  modified_at?: string;
  age_hours?: number;
  cookie_count?: number;
  domains?: Record<string, number>;
}

export interface GrokVerifyResult {
  status: "ok" | "session_expired" | "no_state" | "error";
  checked_at: string;
  final_url: string;
  note: string;
}

export interface GrokLastRun {
  status: string;
  started_at: string | null;
  total_fetched: number;
  session_expired: boolean;
}

export interface GrokSessionStatus {
  state: GrokStateSummary;
  last_verify: GrokVerifyResult | null;
  last_run: GrokLastRun | null;
  acquire_command: string;
}

async function getJson<T>(path: string): Promise<T> {
  const r = await fetch(path, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`HTTP ${r.status}: ${path}`);
  return r.json() as Promise<T>;
}

// Grok タスク定義 (外部 Grok 側スケジュールタスクの写し、2026-08-15)。
// SSoT は Grok 側 — kuebiko は記録用の写しを DB 版保存するのみ。
export interface GrokTaskDef {
  id: string;
  name: string;
  schedule: string;
  window: string;
  prompt: string;
  note: string;
  synced_at: string;
}

export interface GrokTasksResponse {
  tasks: GrokTaskDef[];
  version?: number;
  saved_at?: string;
}

export const grokApi = {
  sessionStatus: () => getJson<GrokSessionStatus>("/api/v1/grok/session"),
  verify: async (): Promise<GrokVerifyResult> => {
    const r = await fetch("/api/v1/grok/session/verify", {
      method: "POST",
      credentials: "same-origin",
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json() as Promise<GrokVerifyResult>;
  },
  tasks: () => getJson<GrokTasksResponse>("/api/v1/grok/tasks"),
  saveTasks: async (tasks: GrokTaskDef[]): Promise<{ saved: boolean; version: number }> => {
    const r = await fetch("/api/v1/grok/tasks", {
      method: "PUT",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tasks }),
    });
    if (!r.ok) {
      const detail = await r.text().catch(() => "");
      throw new Error(`HTTP ${r.status}${detail ? `: ${detail}` : ""}`);
    }
    return r.json() as Promise<{ saved: boolean; version: number }>;
  },
};
