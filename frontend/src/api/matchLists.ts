// 語彙拡張② マッチリスト API client。backend: src/ui/api/match_lists.py。

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

export interface MatchList {
  name: string;
  description: string;
  terms: string[];
}

export const matchListsApi = {
  get: (): Promise<{ lists: MatchList[] }> => getJson("/api/v1/match-lists"),
  save: (lists: MatchList[]): Promise<{ saved: boolean; count: number; version: number }> =>
    postJson("/api/v1/match-lists", { lists }),
};
