// チャンネル管理 API client (チャンネル config-driven 化 C3 + 設定・死活の画面統合 P1)。
// バックエンドは src/ui/api/channels.py。webhook URL は per-channel API で .env に保存し、
// レスポンスは常にマスク値のみ (平文は返らない)。

export interface ChannelDef {
  id: string;
  label: string;
  webhook_env_key: string;
  enabled: boolean;
  routable: boolean;
  // 通知再設計: Discord に配信するか。false = 保存のみ (web-only)。既定 true。
  push: boolean;
  fallback: string | null;
  order: number;
}

export interface ChannelsResponse {
  channels: ChannelDef[];
  builtin_ids: string[];
  rule_refs: Record<string, boolean>;
  webhook_set: Record<string, boolean>;
  // channel id → マスク済み webhook URL (未設定は空文字)
  webhook_masked: Record<string, string>;
}

export type WebhookHealthStatus = "ok" | "warning" | "error";

export interface ChannelsHealthResponse {
  checks: Record<string, { status: WebhookHealthStatus; detail: string }>;
  error: string | null;
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

export const channelsApi = {
  get: () => getJson<ChannelsResponse>("/api/v1/channels"),
  save: (channels: ChannelDef[]) =>
    postJson<{ saved: boolean; count: number; version: number }>("/api/v1/channels", {
      channels,
    }),
  // webhook 疎通 (GET 検証・実投稿なし)
  health: () => getJson<ChannelsHealthResponse>("/api/v1/channels/health"),
  // 投稿先 URL を .env に保存 (空文字 = 削除、即時反映)
  saveWebhook: (channelId: string, url: string) =>
    postJson<{ saved: boolean; webhook_set: boolean; webhook_masked: string }>(
      `/api/v1/channels/${encodeURIComponent(channelId)}/webhook`,
      { url },
    ),
};
