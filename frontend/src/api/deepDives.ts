// 週次深掘りの閲覧 API client (2026-09-27)。read-only。

export interface DeepDiveSelection {
  article_id: string;
  title: string;
  url: string;
  feed_title: string;
  importance: string;
  composite: number;
  pir: number;
  roi: number;
}

export interface DeepDiveWeek {
  period_label: string;
  generated_at: string;
  candidate_count: number;
  recap_text: string;
  selections: DeepDiveSelection[];
}

export async function fetchDeepDives(weeks = 8): Promise<{ items: DeepDiveWeek[]; total: number }> {
  const r = await fetch(`/api/v1/deep-dives?weeks=${weeks}`, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`HTTP ${r.status}: /api/v1/deep-dives`);
  return r.json() as Promise<{ items: DeepDiveWeek[]; total: number }>;
}
