import type {
  ActorDetail,
  AssistantChatResponse,
  AssistantTurn,
  BriefContextResponse,
  DailyBriefDetailResponse,
  DailyBriefMetasResponse,
  DailyBriefsResponse,
  ForecastResponse,
  NationsResponse,
  PMESIIResponse,
  RetrospectResponse,
  SituationResponse,
  SnapshotResponse,
  SynthesisResponse,
  ThreatsResponse,
} from "./types";

const BASE = "/api/v1/intel-graph";

function qs(params: Record<string, string | number | boolean | undefined>): string {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === "" || v === false) continue;
    u.set(k, String(v));
  }
  const s = u.toString();
  return s ? `?${s}` : "";
}

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`HTTP ${r.status}: ${path}`);
  return (await r.json()) as T;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}: ${path}`);
  return (await r.json()) as T;
}

export interface CommonFilters {
  time?: string | number;
  nation?: string;
  family?: string;
  japan_only?: boolean;
  high_only?: boolean;
  search?: string;
}

function commonQs(f: CommonFilters) {
  return qs({
    time: f.time,
    nation: f.nation,
    family: f.family,
    japan_only: f.japan_only ? "1" : "",
    high_only: f.high_only ? "1" : "",
    search: f.search,
  });
}

export const api = {
  snapshot(f: CommonFilters = {}) {
    return get<SnapshotResponse>(`${BASE}/snapshot${commonQs(f)}`);
  },
  threats(f: CommonFilters = {}) {
    return get<ThreatsResponse>(`${BASE}/threats${commonQs(f)}`);
  },
  actorDetail(actorId: string, time: string | number = "30") {
    return get<ActorDetail>(`${BASE}/threats/actor/${encodeURIComponent(actorId)}${qs({ time })}`);
  },
  pmesii(time: string | number = "30", axis = "") {
    return get<PMESIIResponse>(`${BASE}/pmesii${qs({ time, axis })}`);
  },
  situationNations(time: string | number = "90") {
    return get<NationsResponse>(`${BASE}/situation/nations${qs({ time })}`);
  },
  situation(nation = "", time: string | number = "90") {
    return get<SituationResponse>(`${BASE}/situation${qs({ nation, time })}`);
  },
  synthesis(periodType: "daily" | "weekly" | "monthly" = "weekly") {
    return get<SynthesisResponse>(`${BASE}/synthesis${qs({ period_type: periodType })}`);
  },
  forecast(weeks: number = 8) {
    return get<ForecastResponse>(`${BASE}/forecast${qs({ weeks })}`);
  },
  retrospect(weeksAgo: number = 1) {
    return get<RetrospectResponse>(`${BASE}/retrospect${qs({ weeks_ago: weeksAgo })}`);
  },
  // ブリーフ閲覧時の補足コンテキスト (時間軸統合 P2/P3): 24h 活動アクター + 今週の予測
  briefContext(until: string) {
    return get<BriefContextResponse>(`${BASE}/brief-context${qs({ until })}`);
  },
  dailyBriefs(limit: number = 30) {
    return get<DailyBriefsResponse>(`${BASE}/daily-briefs${qs({ limit })}`);
  },
  // 一覧サイドバー用メタのみ (60 件全文 ~2MB の over-fetch を避ける)
  dailyBriefMetas(limit: number = 30) {
    return get<DailyBriefMetasResponse>(`${BASE}/daily-briefs${qs({ limit, meta_only: 1 })}`);
  },
  // 選択時に 1 件だけ本文込みで取得 (react-query が id 単位でキャッシュ)
  dailyBrief(id: number) {
    return get<DailyBriefDetailResponse>(`${BASE}/daily-briefs/${id}`);
  },
  // 分析チャット (2026-07-12): full instance のみ (readonly は POST 403)。
  // model: 会話単位の override (空 = dialog ティア既定)
  assistantChat(message: string, history: AssistantTurn[], model = "") {
    return post<AssistantChatResponse>("/api/v1/assistant/chat", { message, history, model });
  },
};
