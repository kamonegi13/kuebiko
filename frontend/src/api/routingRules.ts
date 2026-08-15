// routing rules API client (案 B / Stage 2-3)。詳細は src/ui/api/routing_rules.py。

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

// when 条件は再帰的な dict (フロントでは unknown として扱い、editor 層で解釈する)。
export type WhenCondition = Record<string, unknown>;

export interface RoutingRule {
  id: string;
  // 表示名 (backend の routing_rules.yaml / DB ルールセットが持つ label)。
  // 未設定のルール (旧版・自作) もあるため optional。UI は label || id を表示する。
  label?: string;
  channel: string;
  when: WhenCondition;
}

// 語彙統一 (2026-06-14): 型付きプロパティ・カタログ (backend PROPERTY_CATALOG の serialize)。
export type PropertyKind = "boolean" | "str" | "set" | "number";

export interface PropertySpec {
  id: string;
  kind: PropertyKind;
  group: string;
  label: string;
  ops: string[];
  domain: string;
  values: string[]; // enum/set の選択肢 (boolean/number は空)
}

export interface RoutingVocab {
  channels: string[];
  properties: PropertySpec[];
  config_lists: string[]; // category の in_config が参照できる list 名
}

export interface RoutingRulesResponse {
  rules: RoutingRule[];
  engine_enabled: boolean;
  vocabulary: RoutingVocab;
}

export interface PreviewScenario {
  scenario: string;
  current_channel: string;
  current_rule: string;
  proposed_channel: string;
  proposed_rule: string;
  changed: boolean;
}

export interface PreviewResponse {
  scenarios: PreviewScenario[];
  changed_count: number;
  total: number;
  errors: string[];
}

export const routingRulesApi = {
  get: () => getJson<RoutingRulesResponse>("/api/v1/routing-rules"),
  save: (rules: RoutingRule[]) =>
    postJson<{ saved: boolean; count: number }>("/api/v1/routing-rules", { rules }),
  preview: (rules: RoutingRule[]) =>
    postJson<PreviewResponse>("/api/v1/routing-rules/preview", { rules }),
};
