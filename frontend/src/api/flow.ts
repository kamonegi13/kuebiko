// 情報フロー API client (SIR → 重要度 → 配信ルール → チャンネル の流れ)。
// バックエンドは src/ui/api/flow.py。

import type { RoutingRule } from "./routingRules";

export interface FlowPir {
  id: string;
  title: string;
  enabled: boolean;
  target_importance: string;
  matched_total: number;
  by_importance: Record<string, number>;
  by_channel: Record<string, number>;
}

export interface FlowEdge {
  importance: string;
  channel: string;
  count: number;
}

export interface FlowChannel {
  id: string;
  label: string;
  enabled: boolean;
  routable: boolean;
  posted_count: number;
}

export interface FlowResponse {
  period_days: number;
  engine_enabled: boolean;
  total_articles: number;
  importance_totals: Record<string, number>;
  importance_channel: FlowEdge[];
  pirs: FlowPir[];
  rules: RoutingRule[];
  channels: FlowChannel[];
}

export const flowApi = {
  get: async (days: number): Promise<FlowResponse> => {
    const r = await fetch(`/api/v1/flow?days=${days}`, { credentials: "same-origin" });
    if (!r.ok) throw new Error(`HTTP ${r.status}: /api/v1/flow`);
    return r.json() as Promise<FlowResponse>;
  },
};

// 重要度の再設計 (2026-10-03、記録のみ)。いまの重要度 × 深刻さ・関連性の件数。
export interface ImportanceV2Cell {
  importance: string;
  severity: "S3" | "S2" | "S1" | null;
  strategic_weight: "heavy" | "moderate" | "light" | null;
  relevant: boolean;
  count: number;
}

export interface ImportanceV2Response {
  period_days: number;
  rule_version: string;
  cells: ImportanceV2Cell[];
}

export const importanceV2Api = {
  get: async (days: number): Promise<ImportanceV2Response> => {
    const r = await fetch(`/api/v1/flow/importance-v2?days=${days}`, { credentials: "same-origin" });
    if (!r.ok) throw new Error(`HTTP ${r.status}: /api/v1/flow/importance-v2`);
    return r.json() as Promise<ImportanceV2Response>;
  },
};
