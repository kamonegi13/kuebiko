// 日本重要インフラ 脅威 posture board API クライアント (近接度ラダー版)。

async function getJson<T>(path: string): Promise<T> {
  const r = await fetch(path, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`HTTP ${r.status}: ${path}`);
  return r.json() as Promise<T>;
}

export type SystemDomain = "ot" | "boundary" | "it" | "unknown";
export type Precision = "sector_only" | "operator_identified" | "system_identified";
export type ItemKind = "observed" | "potential" | "actor" | "readacross";

/** 近接度ラダー: 脅威が当分野へどこまで来ているか (静穏 → 制御系接近)。 */
export type StageId = "quiet" | "indication" | "targeting" | "intrusion_it" | "ot_approach";
export type ActiveStageId = Exclude<StageId, "quiet">;

export const STAGE_ORDER: StageId[] = [
  "quiet",
  "indication",
  "targeting",
  "intrusion_it",
  "ot_approach",
];
/** マトリクス列 (左→右にエスカレーション)。 */
export const MATRIX_COLUMNS: ActiveStageId[] = [
  "indication",
  "targeting",
  "intrusion_it",
  "ot_approach",
];

/** 事業者判定の層: designated=指定事業者名簿 / type=事業者型 (種別サフィックス)。 */
export type OperatorTier = "designated" | "type" | null;

export interface BoardItem {
  id: string;
  kind: ItemKind;
  status: "actual" | "potential";
  title: string;
  operator?: string | null;
  operators?: string[];
  operator_tier?: OperatorTier;
  actor?: string | null;
  nation?: string | null;
  allies?: string | null;
  system_domain: SystemDomain;
  precision: Precision;
  importance?: string | null;
  event_date?: string | null;
  cves?: string[];
  kev?: boolean;
  url?: string | null;
  confidence: "high" | "medium" | "low";
  report_count?: number;
  intent?: string | null;
  operator_id?: string | null;
  /** 集中ノード (清算・決済・取引の単一障害点): 侵害が分野横断にカスケードする。 */
  systemic?: boolean;
}

export type IntentId = "prepositioning" | "disruption" | "espionage" | "subversion" | "coercion";

/** 世界コーパスの国家アクター行動 (victim 非依存の先行指標)。 */
export interface ThreatBehavior {
  id: string;
  actor: string;
  nation: string;
  intent: IntentId;
  articles: number;
  jp_targeted: boolean;
  spike: boolean;
  is_new: boolean;
  ics: boolean;
}

/** 横断キャンペーン: 同一アクターが 3+ 分野で事前潜伏/破壊 = 協調作戦の兆候。 */
export interface Campaign {
  actor: string;
  nation: string;
  intent: IntentId;
  sector_ids: string[];
  sectors: string[];
  articles: number;
  jp_targeted: boolean;
  spike: boolean;
}

export interface SectorPosture {
  nisc_sector: string;
  label: string;
  stage: StageId;
  prev_stage: StageId | null;
  delta: -1 | 0 | 1 | null;
  headline: string;
  quiet_not_safe: boolean;
  systemic_hit: boolean;
  /** 暗域の定量化: 指定 M 事業者中、窓内で観測された distinct 指定事業者 K。 */
  designated_total: number;
  designated_observed: number;
  coverage: { tier: "thin" | "medium" | "rich"; source_count: number; note: string };
  system_domain_mix: { ot: number; boundary: number; it: number; unknown: number };
  stages: Record<ActiveStageId, BoardItem[]>;
  /** 周辺観測: org 特定済みだが名簿/型に合致しない非 CI 報道。段階を駆動しない。 */
  peripheral: BoardItem[];
  /** 行動中心レーン: 世界の国家アクター行動 (JP 観測が静穏でもここに出る先行指標)。 */
  threat_behavior: ThreatBehavior[];
  world_active: boolean;
  counts: Record<ActiveStageId, number> & {
    nonthreat: number;
    peripheral: number;
    behavior: number;
  };
}

export interface BoardChange {
  nisc_sector: string;
  label: string;
  from_stage: StageId;
  to_stage: StageId;
  direction: "up" | "down";
  driver: string;
}

// 段C (2026-07-13): 常設情報要求 posture カード — 台帳 (situations/revisions) 由来の
// 第 3 の独立レンズ。ラダー/世界行動には折り込まない。
export interface PostureTrajectoryPoint {
  rev: number;
  at: string;
  confidence: string; // high / moderate / low
  delta_type: string;
  note: string;
}

export interface PostureCard {
  situation_id: string;
  nation: string;
  nation_label: string;
  question: string;
  assessed: boolean;
  claim: string;
  leading_hypothesis: string;
  leading_label: string;
  confidence: string;
  confidence_basis: string;
  delta_type: string;
  assessed_at: string;
  evidence_related_30d: number;
  evidence_direct_30d: number;
  trajectory: PostureTrajectoryPoint[];
}

export interface JpCiBoardResponse {
  window_days: number | null;
  generated_at: string;
  note: string;
  stage_labels: Record<StageId, string>;
  system_domain_labels: Record<SystemDomain, string>;
  intent_labels: Record<IntentId, string>;
  changes: BoardChange[];
  campaigns: Campaign[];
  sectors: SectorPosture[];
  posture: PostureCard[];
}

export const jpciApi = {
  board: (days: number) => getJson<JpCiBoardResponse>(`/api/v1/jp-ci-board?days=${days}`),
};

// ---------- 指定事業者名簿 (DB SSoT, UI 編集) ----------

export interface OperatorEntry {
  id: string;
  canonical: string;
  aliases: string[];
  sector: string;
}

export interface OperatorsResponse {
  operators: OperatorEntry[];
  sectors: Record<string, string>;
  count: number;
  version: number | null;
}

export const jpciOperatorsApi = {
  list: () => getJson<OperatorsResponse>("/api/v1/jp-ci-operators"),
  save: async (operators: OperatorEntry[]): Promise<{ ok: boolean; version: number }> => {
    const r = await fetch("/api/v1/jp-ci-operators", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ operators }),
    });
    if (!r.ok) {
      const body = (await r.json().catch(() => null)) as {
        detail?: { errors?: string[] } | string;
      } | null;
      const detail = body?.detail;
      const msg =
        typeof detail === "object" && detail?.errors
          ? detail.errors.join(" / ")
          : `HTTP ${r.status}`;
      throw new Error(msg);
    }
    return r.json() as Promise<{ ok: boolean; version: number }>;
  },
};
