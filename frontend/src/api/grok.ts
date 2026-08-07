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
};
