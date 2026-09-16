import { create } from "zustand";

export type Tab = "synthesis" | "pmesii" | "threats" | "operations" | "forecast";
export type PeriodType = "daily" | "weekly" | "monthly";
export type OperationsView = "taxonomy" | "editorial";
// 現況の 3 面 (2026-08-29 に再編)。**役割で分ける** — 利用者が何をしているかで切る。
//   read   = 読む   (全体総括 + SIR ごとの Spotlight を 1 本に)
//   basis  = 根拠   (ACH / 証拠 / 分析トレードクラフト)
//   review = 点検   (予測スコアカード / 情勢台帳)
// 旧 global/spotlight/ledger は、読み物と根拠と点検が同じ画面に縦積みされ、
// 実測でページの 92% が根拠・点検、読むものは 8% だった。
export type SynthesisView = "read" | "basis" | "review";

export interface FilterState {
  tab: Tab;
  time: string;              // "7" | "30" | "90" | "365"
  nation: string;
  family: string;
  japan_only: boolean;
  high_only: boolean;
  search: string;
  actor: string;             // selected actor in threats tab
  axis: string;              // focused axis in pmesii tab
  period_type: PeriodType;   // synthesis tab
  op: OperationsView;        // operations tab sub-view
  synthesisView: SynthesisView; // 現況: 読む / 根拠 / 点検

  setTab: (t: Tab) => void;
  setFilter: <K extends keyof FilterState>(k: K, v: FilterState[K]) => void;
  toggleChip: (k: "japan_only" | "high_only") => void;
  selectActor: (id: string) => void;
  selectAxis: (id: string) => void;
  setPeriodType: (p: PeriodType) => void;
  setOp: (op: OperationsView) => void;
  setSynthesisView: (v: SynthesisView) => void;
  reset: () => void;
}

const initial = {
  tab: "synthesis" as Tab,
  time: "30",
  nation: "",
  family: "",
  japan_only: false,
  high_only: false,
  search: "",
  actor: "",
  axis: "",
  period_type: "weekly" as PeriodType,
  op: "taxonomy" as OperationsView,
  synthesisView: "read" as SynthesisView,
};

// URL hash <-> state sync (bookmark / share / back-button 対応)。
// 値が無いキーは含めない (含めると ``{...initial, ...fromHash}`` で initial を
// undefined で上書きしてしまい、tab: "synthesis" がデフォルト適用されなくなる)。
function parseHash(): Partial<FilterState> {
  if (typeof window === "undefined") return {};
  const h = window.location.hash.startsWith("#") ? window.location.hash.slice(1) : "";
  const p = new URLSearchParams(h);
  const out: Partial<FilterState> = {};
  // tab は pathname (/app/intel/<tab>) が SSoT。hash には載せない (二重符号化の解消)。
  const period = p.get("period_type");
  if (period && ["daily", "weekly", "monthly"].includes(period)) {
    out.period_type = period as PeriodType;
  }
  const op = p.get("op");
  if (op && ["taxonomy", "editorial"].includes(op)) {
    out.op = op as OperationsView;
  }
  const time = p.get("time");
  if (time) out.time = time;
  const nation = p.get("nation");
  if (nation) out.nation = nation;
  const family = p.get("family");
  if (family) out.family = family;
  if (p.get("japan_only") === "1") out.japan_only = true;
  if (p.get("high_only") === "1") out.high_only = true;
  const search = p.get("search");
  if (search) out.search = search;
  const actor = p.get("actor");
  if (actor) out.actor = actor;
  const axis = p.get("axis");
  if (axis) out.axis = axis;
  // situation deep link (#situation=<id>): 台帳ビューへ強制。id 自体は LedgerView が
  // mount 時に hash から直接消費する (filter state には持たない — 1 回限りの着地情報)。
  // 旧 URL (?situation=1) は台帳 = 点検面へ。
  if (p.get("situation")) out.synthesisView = "review";
  return out;
}

function buildHash(state: FilterState): string {
  const p = new URLSearchParams();
  // tab は pathname が SSoT のため hash には含めない。
  if (state.time && state.time !== "30") p.set("time", state.time);
  if (state.nation) p.set("nation", state.nation);
  if (state.family) p.set("family", state.family);
  if (state.japan_only) p.set("japan_only", "1");
  if (state.high_only) p.set("high_only", "1");
  if (state.search) p.set("search", state.search);
  if (state.actor) p.set("actor", state.actor);
  if (state.axis) p.set("axis", state.axis);
  if (state.period_type && state.period_type !== "weekly") p.set("period_type", state.period_type);
  if (state.op && state.op !== "taxonomy") p.set("op", state.op);
  return "#" + p.toString();
}

export const useFilters = create<FilterState>((set, get) => {
  // initial: merge defaults with URL hash
  const fromHash = parseHash();
  const initialState = { ...initial, ...fromHash };

  const syncHash = () => {
    if (typeof window === "undefined") return;
    const next = buildHash(get());
    if (window.location.hash !== next && !(window.location.hash === "" && next === "#")) {
      // avoid pushing identical hash
      history.replaceState(null, "", next || "#");
    }
  };

  // hashchange listener (browser back / share link)
  if (typeof window !== "undefined") {
    window.addEventListener("hashchange", () => {
      const parsed = parseHash();
      set((s) => ({ ...s, ...parsed }));
    });
  }

  return {
    ...initialState,
    setTab: (t) => {
      set((s) => {
        const next: Partial<FilterState> = { tab: t };
        if (t !== "threats") next.actor = "";
        if (t !== "pmesii") next.axis = "";
        return { ...s, ...next };
      });
      syncHash();
    },
    setFilter: (k, v) => {
      set((s) => ({ ...s, [k]: v, ...(k === "search" ? { actor: "" } : {}) }));
      syncHash();
    },
    toggleChip: (k) => {
      set((s) => ({ ...s, [k]: !s[k] }));
      syncHash();
    },
    selectActor: (id) => {
      set((s) => ({ ...s, tab: "threats", actor: id }));
      syncHash();
    },
    selectAxis: (id) => {
      set((s) => ({ ...s, tab: "pmesii", axis: id }));
      syncHash();
    },
    setPeriodType: (p) => {
      set((s) => ({ ...s, period_type: p }));
      syncHash();
    },
    setOp: (op) => {
      set((s) => ({ ...s, op }));
      syncHash();
    },
    setSynthesisView: (v) => {
      // view 切替は transient (hash 非同期。reload で global に戻るのは従来挙動どおり)
      set((s) => ({ ...s, synthesisView: v }));
    },
    reset: () => {
      set(initial);
      syncHash();
    },
  };
});
