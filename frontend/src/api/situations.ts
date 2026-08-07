// 情勢台帳 (Situation Ledger) API client。read-only (書込は synthesis run のみ)。

async function getJson<T>(path: string): Promise<T> {
  const r = await fetch(path, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`HTTP ${r.status}: ${path}`);
  return r.json() as Promise<T>;
}

// delta_type → 日本語ラベル。render 時は vocab "delta_type" (vocabLabel) を SSoT に解決する。
// この静的マップは未移行の JpCiBoardPage (board posture カード) の後方互換でのみ残置。
// delta_type は backend 配信 vocab (delta_type) へ移行済み。静的マップを再生しないこと。

export interface SituationRevision {
  rev: number;
  claim: string;
  claim_type: string;
  leading_hypothesis: string;
  confidence: string;
  confidence_basis: string;
  hypotheses: { hypothesis: string; consistent: number; inconsistent: number; verdict: string }[];
  assumptions: string[];
  missing: string[];
  indicators: string[];
  implication: string;
  delta_type: string;
  delta_note: string;
  created_at: string;
}

export interface SituationSummary {
  kind?: string; // event / standing (常設情報要求)
  situation_id: string;
  title: string;
  domain: string;
  status: string;
  pir_ids: string[];
  opened_at: string;
  last_evidence_at: string;
  evidence_count: number;
  // ACH が評価済み (引用済み) の証拠件数。evidence_count との差 = 割当のみの未評価。
  assessed_count?: number;
  salience: number;
  latest: SituationRevision | null;
}

export interface SituationEvidence {
  article_id: string;
  polarity: string;
  attribution_basis: string;
  excerpt: string;
  source_tier: string;
  added_at: string;
  assigned_by: string;
  // 状態分離 (2026-07-16): 空文字 = 未読 / 未評価。polarity/excerpt は assessed_at
  // が入っている行でのみ有意 (割当だけの行を「中立の証拠」と混同しない)。
  read_at?: string;
  assessed_at?: string;
  article_title?: string;
  article_url?: string;
}

export interface SituationRelation {
  a_id: string;
  b_id: string;
  rel_type: string;
  basis: string;
  other_title: string;
}

export interface SituationDetail {
  situation: {
    situation_id: string;
    title: string;
    domain: string;
    status: string;
    anchors: string[];
    pir_ids: string[];
    opened_at: string;
    last_evidence_at: string;
  };
  revisions: SituationRevision[];
  evidence: SituationEvidence[];
  relations: SituationRelation[];
}

export const situationsApi = {
  list: (status = "active,dormant") =>
    getJson<{ situations: SituationSummary[]; total: number }>(
      `/api/v1/situations?status=${encodeURIComponent(status)}`,
    ),
  detail: (id: string) => getJson<SituationDetail>(`/api/v1/situations/${encodeURIComponent(id)}`),
};
