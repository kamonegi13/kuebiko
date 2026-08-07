// プロダクト配信ルーティング API client (通知再設計)。
// backend: src/ui/api/product_routing.py。curated product (朝刊/夕刊・recap・synthesis・spotlight)
// の配信先 channel を情報フローから編集する。

export interface ProductRoutingChannelOption {
  id: string;
  label: string;
  push: boolean; // false = 保存のみ (web-only)
}

export interface ProductRoutingResponse {
  routing: Record<string, string>; // product_id -> channel_id
  labels: Record<string, string>; // product_id -> 表示ラベル
  builtin: Record<string, string>; // 既定マッピング
  channels: ProductRoutingChannelOption[];
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

export const productRoutingApi = {
  get: () => getJson<ProductRoutingResponse>("/api/v1/product-routing"),
  save: (routing: Record<string, string>) =>
    postJson<{ saved: boolean; count: number; version: number }>("/api/v1/product-routing", {
      routing,
    }),
};
