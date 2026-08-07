import {
  useFilters,
  type Tab,
  type FilterState,
  type PeriodType,
  type OperationsView,
  type SynthesisView,
} from "../state/filters";
import { pageContainer } from "./Page";
import type { SnapshotResponse } from "../api/types";
import { SynthesisTab } from "./tabs/SynthesisTab";
import { PMESIITab } from "./tabs/PMESIITab";
import { ThreatsTab } from "./tabs/ThreatsTab";
import { OperationsTab } from "./tabs/OperationsTab";
import { ForecastTab } from "./tabs/ForecastTab";
import { DiscoveryPanel } from "./DiscoveryPanel";

// 各ページの「実際に効く」コントロールだけを並べる定義 (旧・全ページ共通ツールバーを廃止)。
const TIMES: { v: string; label: string }[] = [
  { v: "7", label: "7d" },
  { v: "30", label: "30d" },
  { v: "90", label: "90d" },
  { v: "365", label: "1y" },
];
const PERIODS: { v: PeriodType; label: string }[] = [
  { v: "daily", label: "日次" },
  { v: "weekly", label: "週次" },
  { v: "monthly", label: "月次" },
];
const VIEWS: { v: SynthesisView; label: string }[] = [
  { v: "global", label: "全体総括" },
  { v: "spotlight", label: "Spotlight" },
  { v: "ledger", label: "情勢台帳" },
];
const OPS: { v: OperationsView; label: string }[] = [
  { v: "taxonomy", label: "分類語彙" },
  { v: "editorial", label: "論調" },
];

// Intel ビューの共有レイアウト。表示ビューは左 nav 由来の route (`tab` prop) が決める。
// 上部は「ページ自前のコントロールバー」: そのページで実際に効くコントロールだけを、
// 脅威マップ上部と同じ全幅フラット border-b 様式で出す (各ページの特性に適合)。
// ページ名は TopBar の breadcrumb が出すため、ここでは見出しを重複表示しない。
export function Shell({ snapshot, tab }: { snapshot?: SnapshotResponse; tab: Tab }) {
  const f = useFilters();
  const families = snapshot?.families || {};
  const nations = snapshot?.nations || [];
  const controls = renderTabControls(tab, f, nations, families);

  return (
    <div className="flex flex-col">
      {controls && <ControlBar>{controls}</ControlBar>}

      {/* コンテンツは中央 container 内 (バーのみ全幅、本文は従来の幅・余白) */}
      <div className={`${pageContainer("wide")} space-y-2`}>
        <div className="animate-fade-in pt-1 min-h-[480px]">
          {tab === "synthesis" && <SynthesisTab />}
          {tab === "pmesii" && <PMESIITab />}
          {tab === "threats" && <ThreatsTab actorLookup={snapshot?.actor_lookup || {}} />}
          {tab === "forecast" && <ForecastTab />}
          {tab === "operations" && <OperationsTab />}
        </div>

        {/* Discovery panel (新規/spike アクター発見。既定は折りたたみ、threats で展開) */}
        {snapshot && <DiscoveryPanel snapshot={snapshot} />}
      </div>
    </div>
  );
}

// ページごとのコントロール集合。何も無いページ (forecast) は null → バー自体を出さない。
function renderTabControls(
  tab: Tab,
  f: FilterState,
  nations: string[],
  families: SnapshotResponse["families"],
): React.ReactNode {
  switch (tab) {
    case "synthesis":
      // 現況: 期間 (日/週/月) と全体総括/Spotlight が本質。time/国/family 等は効かないので出さない。
      return (
        <>
          <Seg items={VIEWS} value={f.synthesisView} onChange={f.setSynthesisView} />
          {f.synthesisView === "global" && (
            <Seg items={PERIODS} value={f.period_type} onChange={f.setPeriodType} />
          )}
        </>
      );
    case "pmesii":
      // 国家情勢: 期間 + 国 のみ効く。
      return (
        <>
          <Seg items={TIMES} value={f.time} onChange={(v) => f.setFilter("time", v)} />
          <NationSelect f={f} nations={nations} />
        </>
      );
    case "threats":
      // 脅威アクター: 全フィルタが効く (現状維持)。
      return (
        <>
          <Seg items={TIMES} value={f.time} onChange={(v) => f.setFilter("time", v)} />
          <NationSelect f={f} nations={nations} />
          <FamilySelect f={f} families={families} />
          <Chip on={f.japan_only} onClick={() => f.toggleChip("japan_only")}>JP</Chip>
          <Chip on={f.high_only} onClick={() => f.toggleChip("high_only")}>高</Chip>
          <span className="ml-auto text-[10.5px] tnum text-fg-subtle bg-surface-2 border border-border-subtle rounded-md h-7 leading-7 px-2">
            系統 <span className="text-fg font-semibold">{Object.keys(families).length}</span>
          </span>
        </>
      );
    case "operations":
      // 運用レビュー: サブビュー切替 (Taxonomy / Editorial) が本質。
      return <Seg items={OPS} value={f.op} onChange={f.setOp} />;
    default:
      return null; // forecast: コントロール無し
  }
}

// 全幅フラット border-b の sticky バー (シームレス)。border/bg は全幅のままだが、
// 内側のコントロールは pageContainer と同じ max-w-[1680px] + 中央寄せ + 同一 padding に
// 制約し、下のコンテンツと左右端を揃える (ワイド画面でのズレ解消)。
function ControlBar({ children }: { children: React.ReactNode }) {
  return (
    <div className="md:sticky md:top-12 z-20 backdrop-blur-md bg-bg/95 border-b border-border-subtle">
      <div className="mx-auto w-full max-w-[1680px] px-4 md:px-6 lg:px-8 py-2 flex flex-wrap items-center gap-1.5 md:gap-2.5">
        {children}
      </div>
    </div>
  );
}

// 汎用 segmented control (期間 / view / op など)。
function Seg<T extends string>({
  items,
  value,
  onChange,
}: {
  items: { v: T; label: string }[];
  value: T;
  onChange: (v: T) => void;
}) {
  return (
    <div className="inline-flex bg-surface-2 border border-border-subtle rounded-md p-0.5 h-7 items-center">
      {items.map((it) => (
        <span
          key={it.v}
          onClick={() => onChange(it.v)}
          className={`px-2.5 h-6 leading-6 cursor-pointer rounded-sm text-sm font-medium tnum transition-all ${
            value === it.v
              ? "bg-surface-overlay text-fg shadow-[0_1px_2px_rgba(0,0,0,0.3)]"
              : "text-fg-muted hover:text-fg hover:bg-surface-3"
          }`}
        >
          {it.label}
        </span>
      ))}
    </div>
  );
}

const SELECT_CLASS =
  "h-7 pl-2.5 pr-7 bg-surface-2 border rounded-md text-sm font-medium cursor-pointer appearance-none bg-[url('data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20width%3D%2210%22%20height%3D%226%22%20viewBox%3D%220%200%2010%206%22%20fill%3D%22none%22%3E%3Cpath%20d%3D%22M1%201l4%204%204-4%22%20stroke%3D%22%239aa3b2%22%20stroke-width%3D%221.5%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%2F%3E%3C%2Fsvg%3E')] bg-no-repeat bg-[right_8px_center] transition-colors";

function NationSelect({ f, nations }: { f: FilterState; nations: string[] }) {
  return (
    <select
      value={f.nation}
      onChange={(e) => f.setFilter("nation", e.target.value)}
      className={`${SELECT_CLASS} ${
        f.nation
          ? "border-accent bg-accent-subtle text-accent-hover font-semibold"
          : "border-border-subtle text-fg hover:bg-surface-3"
      }`}
    >
      <option value="">国</option>
      {nations.map((n) => (
        <option key={n} value={n}>{n.toUpperCase()}</option>
      ))}
    </select>
  );
}

function FamilySelect({ f, families }: { f: FilterState; families: SnapshotResponse["families"] }) {
  return (
    <select
      value={f.family}
      onChange={(e) => f.setFilter("family", e.target.value)}
      className={`${SELECT_CLASS} ${
        f.family
          ? "border-accent bg-accent-subtle text-accent-hover font-semibold"
          : "border-border-subtle text-fg hover:bg-surface-3"
      }`}
    >
      <option value="">系統</option>
      {Object.entries(families).map(([fid, finfo]) => (
        <option key={fid} value={fid}>{finfo.label}</option>
      ))}
    </select>
  );
}

function Chip({ on, onClick, children }: { on: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <span
      onClick={onClick}
      className={`relative pl-7 pr-3 h-7 rounded-md border cursor-pointer text-sm font-medium select-none transition-all inline-flex items-center gap-1 ${
        on
          ? "bg-accent-subtle border-accent text-accent-hover font-semibold"
          : "bg-surface-2 border-border-subtle text-fg-muted hover:bg-surface-3 hover:text-fg hover:border-border-default"
      }`}
    >
      <span
        className={`absolute left-2.5 top-1/2 -translate-y-1/2 w-1.5 h-1.5 rounded-full transition-all ${
          on ? "bg-accent shadow-[0_0_0_3px_rgba(107,136,255,0.18)]" : "bg-border-emphasized"
        }`}
      />
      {children}
    </span>
  );
}
