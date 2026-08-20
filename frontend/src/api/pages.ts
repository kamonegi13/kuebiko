// 残り全 page 用 API client。

import type { FileGroup } from "../components/FileGroupList";

async function getJson<T>(path: string): Promise<T> {
  const r = await fetch(path, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`HTTP ${r.status}: ${path}`);
  return r.json() as Promise<T>;
}

async function postForm<T>(path: string, form: FormData): Promise<T> {
  const r = await fetch(path, { method: "POST", body: form, credentials: "same-origin" });
  if (!r.ok) {
    const text = await r.text();
    throw new Error(`HTTP ${r.status}: ${text}`);
  }
  return r.json() as Promise<T>;
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: "same-origin",
  });
  if (!r.ok) {
    let detail = `HTTP ${r.status}`;
    try {
      const j = (await r.json()) as { detail?: string };
      if (j.detail) detail = j.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return r.json() as Promise<T>;
}

export const pagesApi = {
  // history
  history: (params: { importance?: string; status?: string; limit?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.importance) q.set("importance", params.importance);
    if (params.status) q.set("status", params.status);
    if (params.limit) q.set("limit", String(params.limit));
    return getJson<HistoryResponse>(`/api/v1/history?${q.toString()}`);
  },
  historyDelete: (runId: number) => postForm<{ deleted: boolean }>(`/api/v1/history/${runId}/delete`, new FormData()),
  historyPurge: (days: number) => {
    const f = new FormData();
    f.set("days", String(days));
    return postForm<{ deleted_count: number }>(`/api/v1/history/purge`, f);
  },
  // health
  healthStatus: () => getJson<HealthResponse>("/api/v1/health-status"),
  // subscriptions
  subscriptions: () => getJson<SubscriptionsResponse>("/api/v1/subscriptions"),
  // prompts
  promptsList: () => getJson<{ files: string[]; groups: FileGroup[] }>("/api/v1/prompts"),
  promptsFile: (path: string) => getJson<PromptFile>(`/api/v1/prompts/file?path=${encodeURIComponent(path)}`),
  promptsSave: (path: string, content: string) => {
    const f = new FormData();
    f.set("path", path);
    f.set("content", content);
    return postForm<{ saved: boolean }>("/api/v1/prompts/save", f);
  },
  // config
  config: () => getJson<ConfigResponse>("/api/v1/config"),
  configYaml: (path: string) => getJson<{ path: string; content: string }>(`/api/v1/config/yaml?path=${encodeURIComponent(path)}`),
  configEnvSave: (updates: Record<string, string>) => {
    const f = new FormData();
    for (const [k, v] of Object.entries(updates)) f.set(k, v);
    return postForm<{ saved: boolean; count: number }>("/api/v1/config/env", f);
  },
  configYamlSave: (path: string, content: string) => {
    const f = new FormData();
    f.set("path", path);
    f.set("content", content);
    return postForm<{ saved: boolean }>("/api/v1/config/yaml", f);
  },
  // schedule
  schedule: () => getJson<ScheduleResponse>("/api/v1/schedule"),
  schedulePause: (jobId: string) => postForm(`/api/v1/schedule/${jobId}/pause`, new FormData()),
  scheduleResume: (jobId: string) => postForm(`/api/v1/schedule/${jobId}/resume`, new FormData()),
  scheduleTrigger: (jobId: string) => postForm(`/api/v1/schedule/${jobId}/trigger`, new FormData()),
  scheduleCron: (jobId: string, hour: number, minute: number) => {
    const f = new FormData();
    f.set("hour", String(hour));
    f.set("minute", String(minute));
    return postForm<{ updated: boolean }>(`/api/v1/schedule/${jobId}/cron`, f);
  },
  scheduleUpdateFull: (
    jobId: string,
    payload: { mode: "cron" | "interval"; enabled: boolean; hour?: number; minute?: number; interval_minutes?: number; day_of_week?: string },
  ) => {
    const f = new FormData();
    f.set("mode", payload.mode);
    f.set("enabled", payload.enabled ? "1" : "0");
    if (payload.hour !== undefined) f.set("hour", String(payload.hour));
    if (payload.minute !== undefined) f.set("minute", String(payload.minute));
    if (payload.interval_minutes !== undefined) f.set("interval_minutes", String(payload.interval_minutes));
    if (payload.day_of_week) f.set("day_of_week", payload.day_of_week);
    return postForm<{ updated: boolean }>(`/api/v1/schedule/${jobId}/update_schedule`, f);
  },
  scheduleUpdateSource: (jobId: string, max_articles: number) => {
    const f = new FormData();
    f.set("max_articles", String(max_articles));
    return postForm<{ updated: boolean }>(`/api/v1/schedule/${jobId}/update_source`, f);
  },
  scheduleUpdateDedup: (
    jobId: string,
    payload: { similarity_threshold_hard: number; similarity_threshold_cluster: number; dedup_window_hours_hard: number; dedup_window_hours_cluster: number },
  ) => {
    const f = new FormData();
    f.set("similarity_threshold_hard", String(payload.similarity_threshold_hard));
    f.set("similarity_threshold_cluster", String(payload.similarity_threshold_cluster));
    f.set("dedup_window_hours_hard", String(payload.dedup_window_hours_hard));
    f.set("dedup_window_hours_cluster", String(payload.dedup_window_hours_cluster));
    return postForm<{ updated: boolean }>(`/api/v1/schedule/${jobId}/update_dedup`, f);
  },
  scheduleUpdateThink: (jobId: string, think_enabled: boolean) => {
    const f = new FormData();
    f.set("think_enabled", think_enabled ? "1" : "0");
    return postForm<{ updated: boolean }>(`/api/v1/schedule/${jobId}/update_think`, f);
  },
  scheduleUpdateTriage: (
    jobId: string,
    payload: { enabled: boolean; keep_importance: string[]; max_keep: number },
  ) => {
    const f = new FormData();
    f.set("enabled", payload.enabled ? "1" : "0");
    f.set("max_keep", String(payload.max_keep));
    for (const v of payload.keep_importance) f.append("keep_importance", v);
    return postForm<{ updated: boolean }>(`/api/v1/schedule/${jobId}/update_triage`, f);
  },
  scheduleToggleScraper: (jobId: string, scraper: string, enabled: boolean) => {
    const f = new FormData();
    f.set("enabled", enabled ? "1" : "0");
    return postForm<{ updated: boolean; enabled: boolean }>(
      `/api/v1/schedule/${jobId}/scrapers/${scraper}/toggle`, f,
    );
  },
  // S3: source 信頼度ティアの上書き設定/解除 (tier="auto" で解除 → pattern default に戻る)
  setReliability: (feed_url: string, title: string, tier: ReliabilityTier | "auto") => {
    const f = new FormData();
    f.set("feed_url", feed_url);
    f.set("title", title);
    f.set("tier", tier);
    return postForm<{ applied: string; reliability_tier: string }>(
      "/api/v1/subscriptions/reliability", f,
    );
  },
  // Actor 辞書: 構造化編集 (alias 重複=誤帰属を保存時に検証)
  getActors: () => getJson<ActorsResponse>("/api/v1/actors"),
  updateActor: (id: string, body: ActorEdit) =>
    postJson<{ saved: boolean; actor_id: string }>(
      `/api/v1/actors/${encodeURIComponent(id)}`,
      body,
    ),
  // アクター辞書 Phase1: 行動史 (月次期間行) + actor→situation 逆引き
  actorHistory: (id: string) =>
    getJson<ActorHistoryResponse>(`/api/v1/actors/${encodeURIComponent(id)}/history`),
  // 一覧用バッチ (P2-S8): 最終観測月・主題累計・照合実績のある名前
  actorsObservedSummary: () =>
    getJson<ActorsObservedSummaryResponse>("/api/v1/actors/observed-summary"),
  // 月次行の証拠開示 (D5)
  actorMonthArticles: (id: string, month: string) =>
    getJson<ActorMonthArticlesResponse>(
      `/api/v1/actors/${encodeURIComponent(id)}/history/${encodeURIComponent(month)}/articles`,
    ),
  actorSituations: (id: string) =>
    getJson<ActorSituationsResponse>(`/api/v1/actors/${encodeURIComponent(id)}/situations`),
  // Actors Stage 4: MITRE 同期のレビュー提案 (新規 actor / alias 衝突のみ)
  getActorSync: () => getJson<ActorSyncResponse>("/api/v1/actors/sync"),
  decideActorProposal: (id: number, action: "approve" | "reject") =>
    postJson<{ proposal_id: number }>(`/api/v1/actors/sync/proposals/${id}/${action}`, {}),
  // ブリーフ設定 (source_quality): brief cap + high-threat カテゴリ (raw YAML 不要)
  getSourceQuality: () =>
    getJson<SourceQualityConfig>("/api/v1/config/source-quality"),
  saveSourceQuality: (brief_cap_24h: number, categories: string[]) => {
    const f = new FormData();
    f.set("brief_cap_24h", String(brief_cap_24h));
    f.set("categories", categories.join(","));
    return postForm<{ saved: boolean; brief_cap_24h: number; high_threat_brief_categories: string[] }>(
      "/api/v1/config/source-quality", f,
    );
  },
  // LLM モデルティア割当
  getModelTiers: () => getJson<ModelTiersResponse>("/api/v1/model-tiers"),
  saveModelTiers: (tiers: Record<string, string>, narrativeThink?: string) =>
    postJson<{ saved: boolean; count: number; version: number }>("/api/v1/model-tiers", {
      tiers,
      ...(narrativeThink !== undefined ? { narrative_think: narrativeThink } : {}),
    }),
  // 外部 LLM API キー (.env に保存、即時反映。空文字 = 削除)
  saveAnthropicKey: (api_key: string) =>
    postJson<{ saved: boolean; external_enabled: boolean; anthropic_key_masked: string }>(
      "/api/v1/model-tiers/anthropic-key", { api_key },
    ),
  // Ollama 接続 URL (.env、即時反映。保存直後の疎通結果を同梱)
  saveOllamaUrl: (base_url: string) =>
    postJson<{ saved: boolean; ollama_base_url: string; model_count: number | null; error: string | null }>(
      "/api/v1/model-tiers/ollama-url", { base_url },
    ),
  // システム設定 (LOG_LEVEL / TIMEZONE、.env 保存 + process env 同期)
  configSystemSave: (log_level: string, timezone: string) =>
    postJson<{ saved: boolean; log_level: string; timezone: string }>(
      "/api/v1/config/system", { log_level, timezone },
    ),
  // Claude Code CLI の自己更新 (sidecar volume 内バイナリの差し替え、再起動不要)
  claudeCodeUpdate: () =>
    postJson<{ updated: boolean; before: string; after: string }>(
      "/api/v1/model-tiers/claudecode-update", {},
    ),
  // Claude Code サブスクの長期トークン (.env、空文字 = 削除)
  saveClaudeCodeToken: (token: string) =>
    postJson<{ saved: boolean; token_set: boolean; token_masked: string }>(
      "/api/v1/model-tiers/claudecode-token", { token },
    ),
  // ユーザ定義 LLM 接続先 (OpenAI 互換): 一覧を丸ごと保存 (DB 版履歴)
  saveLlmEndpoints: (endpoints: { name: string; base_url: string }[]) =>
    postJson<{ saved: boolean; count: number; version: number }>(
      "/api/v1/model-tiers/endpoints", { endpoints },
    ),
  // 接続先の API キー (.env、空文字 = 削除)
  saveLlmEndpointKey: (name: string, api_key: string) =>
    postJson<{ saved: boolean; key_set: boolean; key_masked: string }>(
      "/api/v1/model-tiers/endpoint-key", { name, api_key },
    ),
  // taxonomy review
  taxonomyReview: () => getJson<TaxonomyResponse>("/api/v1/taxonomy-review"),
  taxonomyAction: (id: number, action: "accept" | "reject" | "defer") =>
    postForm<{ updated: boolean }>(`/api/v1/taxonomy-review/${id}/${action}`, new FormData()),
  // editorial quality
  editorialQuality: (params: { lookback_days?: number; stance_filter?: string; feed_filter?: string } = {}) => {
    const q = new URLSearchParams();
    if (params.lookback_days) q.set("lookback_days", String(params.lookback_days));
    if (params.stance_filter) q.set("stance_filter", params.stance_filter);
    if (params.feed_filter) q.set("feed_filter", params.feed_filter);
    return getJson<EditorialResponse>(`/api/v1/editorial-quality?${q.toString()}`);
  },
  editorialReview: (data: { article_id: string; original_stance: string; corrected_stance: string; comment: string }) => {
    const f = new FormData();
    f.set("article_id", data.article_id);
    f.set("original_stance", data.original_stance);
    f.set("corrected_stance", data.corrected_stance);
    f.set("comment", data.comment);
    return postForm<{ saved: boolean }>("/api/v1/editorial-quality/review", f);
  },
  // mobile tunnel (Phase Diamond verify-mobile / named tunnel の UI 管理は 2026-07-30 C2)
  accessAudit: (limit = 50) => getJson<AccessAuditResponse>(`/api/v1/access-audit?limit=${limit}`),
  // ops 通知の永続化 (2026-08-21): post_ops_message が送った Discord #ops 通知の記録
  opsNotices: (limit = 50) => getJson<OpsNoticesResponse>(`/api/v1/ops-notices?limit=${limit}`),
  hostWatchdogStatus: () => getJson<HostWatchdogStatus>("/api/v1/host-watchdog/status"),
  hostWatchdogEnable: () =>
    postForm<HostWatchdogStatus>("/api/v1/host-watchdog/enable", new FormData()),
  hostWatchdogDisable: () =>
    postForm<HostWatchdogStatus>("/api/v1/host-watchdog/disable", new FormData()),
  mobileTunnelStatus: () => getJson<MobileTunnelStatus>("/api/v1/mobile-tunnel/status"),
  mobileTunnelEnable: () => postForm<MobileTunnelStatus>("/api/v1/mobile-tunnel/enable", new FormData()),
  mobileTunnelDisable: () => postForm<MobileTunnelStatus>("/api/v1/mobile-tunnel/disable", new FormData()),
  // named tunnel の token / hostname を保存 (token 空は既存維持)。token 生値は API から返らない。
  mobileTunnelSetNamedConfig: (body: { token?: string; hostname?: string }) =>
    postJson<MobileTunnelStatus>("/api/v1/mobile-tunnel/named-config", body),
  mobileTunnelClearNamedConfig: () =>
    postJson<MobileTunnelStatus>("/api/v1/mobile-tunnel/named-config/clear", {}),
};

// 外部公開の認証 (Cloudflare Access) の監査証跡。
// §4 により email は保存していないため、識別は subject_hash (SHA-256 先頭 12 桁)。
export interface AccessAuditEvent {
  at: string;
  event: string; // authenticated / rejected / tier1_write
  subject_hash: string;
  method: string;
  path: string;
  client_ip: string;
  country: string;
  detail: string;
}

export interface AccessAuditResponse {
  events: AccessAuditEvent[];
  count: number;
  auth: { configured: boolean; team_domain: string };
  error?: string;
}

// ops 通知の永続化 (2026-08-21)。post_ops_message は webhook 送信の成否に関わらず
// 必ず 1 行残す (webhook 未設定/不達でも sent=false で残る)。
export interface OpsNotice {
  id: number;
  created_at: string;
  title: string;
  body: string;
  importance: string; // high / medium / low (vocab "importance" で日本語化)
  sent: boolean;
}

export interface OpsNoticesResponse {
  notices: OpsNotice[];
  count: number;
  error?: string;
}

// ホスト常駐 watchdog (OrbStack 無応答の自動復旧)。macOS 固有のため、Linux サーバでは
// スクリプトが即 no-op になり installed=false のまま (導入しなければ何も動かない)。
export interface HostWatchdogStatus {
  enabled: boolean;
  installed: boolean; // 一度でも実行された = LaunchAgent 導入済み
  checked_at: string;
  status: string; // healthy / degraded / recovered / recovery_failed / idle
  detail: string;
  consecutive_failures: number;
  last_recovery_at: string;
  last_recovery_result: string;
  log_tail: string[];
}

export interface MobileTunnelStatus {
  enabled: boolean;
  url: string | null;
  mode: "named" | "quick" | "off";
  hostname: string | null;
  token_set: boolean;
}

// ===== Types =====
export interface HistoryArticle {
  id: number;
  run_id: number;
  article_id: string;
  title: string;
  url: string;
  feed_title: string;
  importance: string;
  category: string;
  status: string;
  posted_channel: string | null;
  published_at: string | null;
  created_at: string | null;
}

export interface HistoryResponse {
  articles: HistoryArticle[];
  recent_runs: { id: number; pipeline: string; status: string; posted: number; error_count: number; dry_run: boolean; started_at: string | null }[];
  filters: { importance: string; status: string };
}

export interface HealthCheck {
  name: string;
  status: string;
  // backend (src/ui/services/health.py の HealthCheck) が返すのは detail 1 本。
  // 以前は message / details を期待していたため死活タブの説明欄が常に空だった。
  detail: string;
}

export interface HealthResponse {
  checks: HealthCheck[];
  load_error: string | null;
}

export interface Subscription {
  feed_id: string;
  title: string;
  url: string;
  html_url: string | null;
  folder_labels: string[];
  // Phase F: rss(=feeds.yaml) / html_scraper(=scrapers.yaml) / sitemap(=watchers.yaml)
  transport?: "rss" | "html_scraper" | "sitemap";
  enabled?: boolean;
  // S3: briefing 信頼度ティア (override → pattern の実効値)
  reliability_tier?: "official" | "research" | "news" | "social" | "state_media" | "unknown";
  // 取得死活 (source_fetch_health)。「stats なし」を取得エラー/取得OK・新着なし に分解する観測点
  health?: SourceFetchHealth | null;
}

export interface SourceFetchHealth {
  consecutive_failures: number;
  last_ok_at: string;
  last_error_at: string;
  last_article_count: number;
  last_error: string;
}

export type ReliabilityTier = "official" | "research" | "news" | "social" | "state_media";

export interface SourceQualityConfig {
  brief_cap_24h: number;
  high_threat_brief_categories: string[];
  available_categories: string[];
}

export interface TierMeta {
  label: string;
  description: string;
  examples: string;
}

// llm_usage 由来の消費集計 (5h窓/今日/7日、provider 別)
export interface LlmUsageWindow {
  calls: number;
  input_tokens: number;
  output_tokens: number;
}
export type LlmUsageWindows = Partial<Record<"window_5h" | "today" | "days7", LlmUsageWindow>>;

// ユーザ定義 LLM 接続先 (OpenAI 互換)。登録済みのものだけ API から返る
export interface LlmEndpointInfo {
  name: string;
  base_url: string;
  key_env: string;
  key_set: boolean;
  key_masked: string;
  models: string[];
  usage: LlmUsageWindows | null;
}

export interface LlmEndpointPreset {
  name: string;
  label: string;
  base_url: string;
}

export interface ModelTiersResponse {
  tiers: Record<string, string>;
  // narrative ティアの拡張思考: "auto" (外部モデル割当時のみ ON) / "off"
  narrative_think: string;
  endpoints: LlmEndpointInfo[];
  endpoint_presets: LlmEndpointPreset[];
  anthropic_usage: LlmUsageWindows | null;
  ollama_base_url: string;
  meta: Record<string, TierMeta>;
  builtin: Record<string, string>;
  available: string[];
  // 外部 LLM (anthropic:*)。ANTHROPIC_API_KEY 未設定時は空 = ローカルのみ
  external: string[];
  external_enabled: boolean;
  // Claude Code サブスク経由 (claudecode:*)。ホスト bridge 疎通時のみ非空
  claude_code: string[];
  claude_code_enabled: boolean;
  claude_code_usage: ClaudeCodeUsage | null;
  // bridge の認証モード: token (UI 管理) / cli-default (ホストログイン)。旧 bridge は null
  claude_code_auth: { token_set: boolean; mode: string } | null;
  // CLI 更新の照合 (npm registry と比較)。available=true で更新ボタンを表示
  claude_code_update: { current: string; latest: string; available: boolean } | null;
  claude_code_bridge_url: string;
  // キーのマスク表示 (先頭4文字+***)。平文は API から返らない
  anthropic_key_masked: string;
  excluded: string[];
  ollama_error: string | null;
}

export interface ClaudeCodeUsageWindow {
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cost_usd_equivalent: number;
}

// bridge が自己観測するサブスク消費 (レート残量 API は無いため消費側を可視化)。
// window は該当期間に消費が無ければ backend が省略する (window_5h が最も欠けやすい) ため
// 全て optional。UI は欠測を "—" 表示にする (2026-07-29: 非 optional の型嘘で undefined.calls
// クラッシュ → モデル設定ページ全体が白画面になっていた)。
export interface ClaudeCodeUsage {
  window_5h?: ClaudeCodeUsageWindow;
  today?: ClaudeCodeUsageWindow;
  days7?: ClaudeCodeUsageWindow;
  last_call_at: string | null;
}

export interface ActorEdit {
  canonical: string;
  aliases: string[];
  mitre_group: string | null;
  nation: string | null;
  sponsor: string | null;
  family: string | null;
  kind: string | null;
  sponsor_org: string | null;
  description: string | null;
  // F6 (2026-07-26): 照合ゲート編集可能化 (origin は死にフィールドのため撤去)
  ambiguous: boolean;
  context_cues: string[];
  // reference 用詳細 (Stage 1)
  summary: string | null;
  motivation: string | null;
  first_seen: string | null;
  target_sectors: string[];
  target_regions: string[];
  associated_malware: string[];
  notable_campaigns: string[];
  references: string[];
}

export interface ActorRecord extends ActorEdit {
  id: string;
  // 既知 TTP ("T1566 Phishing" 形式)。mitre_sync 所有の read-only field (編集不可)
  mitre_ttps: string[];
}

export const ACTOR_KINDS = ["group", "organization", "contractor"] as const;
// actor_kind の日本語ラベルは backend vocab "actor_kind" (vocabLabel) を SSoT に解決する
// (静的 value→label マップは持たない)。consumer: pages/actors/ActorDetail.tsx。

export interface ActorsResponse {
  actors: ActorRecord[];
  families: string[];
}

// アクター辞書 Phase1: 月次行動史 (actor_observed_profile の表示時合算)
export interface ActorMonthRow {
  month: string; // 'YYYY-MM' (JST)
  subject_articles: number;
  distinct_sources: number;
  sectors: Record<string, number>;
  countries: Record<string, number>;
  malware: Record<string, number>;
  ttps: Record<string, number>;
  campaigns: Record<string, number>;
  japan_targeted: number;
  kev_hits: number;
}

export interface ActorHistoryResponse {
  actor_id: string;
  requested_id: string;
  merged_from: string[];
  epoch_month: string;
  months: ActorMonthRow[];
  series: { month: string; subject_articles: number }[];
  // F5: 名前 (canonical/alias) ごとの累計ヒット記事数
  alias_usage: Record<string, number>;
  note: string;
}

export interface ActorSituationRow {
  situation_id: string;
  title: string;
  domain: string;
  status: string;
  kind: string;
  opened_at: string;
  last_evidence_at: string;
}

export interface ActorSituationsResponse {
  actor_id: string;
  situations: ActorSituationRow[];
  total: number;
}

// 月次行の証拠開示 (D5): その月の主題記事のライブ照会
export interface ActorMonthArticle {
  article_id: string;
  title: string;
  url: string;
  feed_title: string | null;
  importance: string | null;
  created_at: string;
  japan_targeted: boolean;
  kev_hit: boolean;
}

export interface ActorMonthArticlesResponse {
  actor_id: string;
  month: string;
  articles: ActorMonthArticle[];
  total: number;
}

// 一覧用バッチ (P2-S8)
export interface ActorObservedSummary {
  last_month: string | null; // 最終観測月 'YYYY-MM' (主題記事ベース)
  subject_total: number;
  matched_names: string[]; // 照合実績のある名前 (F5)
}

export interface ActorsObservedSummaryResponse {
  summaries: Record<string, ActorObservedSummary>;
}

// Actors Stage 4: MITRE 同期レビュー提案 + Actor Recall Layer: コーパス由来の新興アクター候補
export interface ActorSyncProposal {
  id: number;
  proposal_type:
    | "mitre_new_actor"
    | "mitre_alias_conflict"
    | "corpus_emerging_actor"
    | "news_alias";
  mitre_group: string;
  actor_id: string | null;
  // mitre_new_actor: 追加予定の actor dict / mitre_alias_conflict: alias 付替え情報
  // corpus_emerging_actor: 提案 actor dict + _evidence (article_count/signal/sample_titles)
  payload: Record<string, unknown>;
  rationale: string;
  status: string;
  created_at: string;
}

export interface ActorSyncResponse {
  proposals: ActorSyncProposal[];
  pending_count: number;
  synced_actors: number;
  total_actors: number;
}

export interface FeedStats {
  feed_title: string;
  posted_count: number;
  dup_skipped: number;
  alert_count: number;
  brief_count: number;
  watch_count: number;
  high_count: number;
  medium_count: number;
  low_count: number;
  first_seen_at: string | null;
  // Phase C
  quality_score?: number;
  low_contrib_labels?: string[];
  dup_rate?: number;
  watch_only_rate?: number;
  // 本文完全性 (2026-07-27): このソースの全文取得率 / 切り株率。
  full_body_count?: number;
  stump_count?: number;
  full_body_rate?: number;
  stump_rate?: number;
  // 層別健全性 (2026-08-01): feed は取れるが本文抽出で失敗した記事数 (30日)。
  // 「本文取得エラー」グループの分類根拠 (壊れた層を名指しする)
  extract_failed_count?: number;
}

export interface SubscriptionsResponse {
  subscriptions: Subscription[];
  sources_error: string | null;
  stats: Record<string, FeedStats>;
}

export interface PromptFile {
  path: string;
  content: string;
  backup_exists: boolean;
}

export interface ConfigResponse {
  env_keys: string[];
  env_descriptions: Record<string, string>;
  secret_keys: string[];
  env_values: Record<string, string>;
  env_masked: Record<string, string>;
  yaml_files: string[];
  yaml_groups: FileGroup[];
}

export interface ClusterMember {
  name: string;
  enabled: boolean;
  seen_count: number | null;
  last_modified: string | null;
  state_exists: boolean;
}

export interface ScheduleEntry {
  name: string;
  source_type: string;
  max_articles: number;
  schedule_enabled: boolean;
  hour: number | null;
  minute: number | null;
  interval_minutes: number | null;
  day_of_week: string | null;
  triage_enabled: boolean;
  triage_keep_importance: string[];
  triage_max_keep: number;
  think_enabled: boolean;
  similarity_threshold_hard: number;
  similarity_threshold_cluster: number;
  dedup_window_hours_hard: number;
  dedup_window_hours_cluster: number;
  cluster_members: ClusterMember[];
  next_run_at: string | null;
  is_paused: boolean;
}

export interface ScheduleResponse {
  pipelines: ScheduleEntry[];
  load_error?: string;
  scheduler_available?: boolean;
}

export interface TaxonomyProposal {
  id: number;
  proposal_type: string;
  tier: string;
  target_yaml: string;
  target_canonical: string;
  proposed_change: string;
  rationale: string;
  confidence: string;
  evidence_count: number;
  status: string;
  reviewed_at: string;
}

export interface TaxonomyResponse {
  tier_1: TaxonomyProposal[];
  tier_2: TaxonomyProposal[];
  tier_3: TaxonomyProposal[];
  recent_reviewed: TaxonomyProposal[];
}

export interface EditorialArticle {
  article_id: string;
  title: string;
  feed_title: string;
  importance: string;
  category: string;
  posted_channel: string;
  stance: string;
  created_at: string;
  url: string;
  review: { corrected_stance: string; comment: string } | null;
}

export interface FeedRow {
  feed: string;
  counts: Record<string, number>;
  total: number;
  propaganda_ratio: number;
}

export interface EditorialResponse {
  feed_rows: FeedRow[];
  stance_totals: Record<string, number>;
  stances: string[];
  articles: EditorialArticle[];
  lookback_days: number;
  stance_filter: string;
  feed_filter: string;
  total_articles: number;
}
