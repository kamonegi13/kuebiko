// Grok メール受信 (IMAP) API client (設定・死活の画面統合 P2)。
// バックエンドは src/ui/api/grok_mail.py。値は常にマスク値のみ (平文は返らない)。

export interface GrokMailState {
  host_masked: string;
  port: number;
  user_masked: string;
  password_set: boolean;
  configured: boolean;
}

export interface GrokMailHealth {
  status: "ok" | "warning" | "error";
  detail: string;
}

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

export const grokMailApi = {
  get: () => getJson<GrokMailState>("/api/v1/grok-mail"),
  // IMAP ログイン試行 (10 秒 timeout はバックエンド側)
  health: () => getJson<GrokMailHealth>("/api/v1/grok-mail/health"),
  // 空欄フィールドは既存値を維持 (secret 空送信 skip と同じ規約)
  save: (fields: { host?: string; port?: string; user?: string; password?: string }) =>
    postJson<{ saved: boolean } & GrokMailState>("/api/v1/grok-mail", {
      host: fields.host ?? "",
      port: fields.port ?? "",
      user: fields.user ?? "",
      password: fields.password ?? "",
    }),
};
