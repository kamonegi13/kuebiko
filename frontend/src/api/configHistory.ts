// 運用 config の変更履歴 API client。詳細は src/ui/api/config_history.py。

async function getJson<T>(path: string): Promise<T> {
  const r = await fetch(path, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`HTTP ${r.status}: ${path}`);
  return r.json() as Promise<T>;
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const text = await r.text();
    throw new Error(`HTTP ${r.status}: ${text}`);
  }
  return r.json() as Promise<T>;
}

export interface ConfigKeySummary {
  key: string;
  label: string;
  current_version: number | null;
  version_count: number;
}

export interface ConfigVersion {
  version: number;
  note: string;
  created_at: string;
  is_current: boolean;
}

export interface ConfigHistory {
  key: string;
  label: string;
  current_version: number | null;
  versions: ConfigVersion[];
}

export interface ConfigVersionValue {
  key: string;
  version: number;
  value: unknown;
}

export interface RevertResult {
  reverted: boolean;
  key: string;
  from_version: number;
  new_version: number;
}

export const configHistoryApi = {
  listKeys: (): Promise<{ keys: ConfigKeySummary[] }> =>
    getJson("/api/v1/config-history"),
  history: (key: string): Promise<ConfigHistory> =>
    getJson(`/api/v1/config-history/${encodeURIComponent(key)}`),
  version: (key: string, version: number): Promise<ConfigVersionValue> =>
    getJson(`/api/v1/config-history/${encodeURIComponent(key)}/${version}`),
  revert: (key: string, version: number): Promise<RevertResult> =>
    postJson(`/api/v1/config-history/${encodeURIComponent(key)}/revert`, { version }),
};
