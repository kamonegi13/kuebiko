// ダッシュボード概観の集計 API。「変化(前期比) + その変化を駆動した内訳」を 1 バッチで取得。
// count は「報道/観測 件数」(攻撃件数ではない)。

export interface Mover {
  key: string;
  label: string;
  current: number;
  prior: number;
  delta: number;
  // 発見1: 総量正規化したシェア (%) と pp 差。生 delta の volume 汚染を回避。
  share_pct: number;
  share_prior_pct: number;
  share_delta_pp: number;
}

export interface VulnItem {
  cve: string;
  cvss: number | null;
  kev: boolean;
  title: string;
  url: string;
  article_id: string; // in-app 記事詳細 (/app/article/<id>) 用
  created_at: string;
  // 発見4: まとめ記事由来の CVE は title が無意味 → CVE 主体表示に切替。
  is_roundup: boolean;
  nvd_url: string;
}

// 前期比つきカウント KPI (要対応 / 日本標的 等)。
export interface CountKpi {
  current: number;
  prior: number;
  delta: number;
}

// 「今日の要注意」triage 行の 1 件。
export interface AttentionItem {
  kind: "kev" | "new_actor" | "spike" | "japan";
  label: string;
  detail: string;
  href: string;
}

export interface NotableActor {
  actor_id: string;
  canonical: string;
  nation: string;
  tag: "spike" | "new";
  total: number;
  sparkline: string; // server 生成 SVG 文字列
}

export interface OverviewData {
  as_of: string;
  window_days: number;
  importance: {
    high: number;
    medium: number;
    low: number;
    high_prior: number;
    high_delta: number;
  };
  // 要対応 = high∩(KEV悪用/日本標的) + JP ランサム被害 + JP prepositioning (確定)。
  // 生 high でなく decision-bearing な部分集合 (H5 で medium 固定の生成物経路も回収)。
  actionable: CountKpi;
  japan_targeted: CountKpi;
  kev: CountKpi & { new_count: number; new_cves: string[] };
  active_apt: CountKpi & { spiking: number };
  sectors: Mover[];
  regions: Mover[];
  nations: Mover[];
  vulnerabilities: VulnItem[];
  notable_actors: NotableActor[];
  source_confidence: Record<string, number>;
  attention: AttentionItem[];
}

async function getJson<T>(path: string): Promise<T> {
  const r = await fetch(path, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`HTTP ${r.status}: ${path}`);
  return (await r.json()) as T;
}

export const overviewApi = {
  get: (days = 7) => getJson<OverviewData>(`/api/v1/dashboard/overview?days=${days}`),
};
